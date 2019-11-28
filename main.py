'''

Image Classification with finetuning or feature extraction on pretrained resnet-50
10 kind of leaves provided from assistant teachers
Pytorch 1.1.0 & python 3.6

Author: @weiyx16.github.io
weiyx16@mails.tsinghua.edu.cn

adapted from https://pytorch.org/tutorials/beginner/finetuning_torchvision_models_tutorial.html

'''
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
from torchvision import models, transforms
from torch.utils.data import DataLoader
# from torch.utils.data.distributed import DistributedSampler
from distributed import DistributedSampler
import torch.distributed as distributed
from torch.nn.parallel import DistributedDataParallel as DDP
import matplotlib.pyplot as plt
import time
import os
from argparse import ArgumentParser
import copy
from PIL import Image
from tqdm import tqdm
from datetime import date
from MyDataset import CustomTensorDataset
from LeNet import LeNet
from ResNet import ResNet18

print("PyTorch Version: ",torch.__version__)

# hyperparameter
# Top level data directory. Here we assume the format of the directory conforms to the ImageFolder structure

train_data_dir = r"./data/train.npy"
train_gt_dir = r"./data/train.csv"
validation_data_dir = r"./data/validation.npy"
validation_gt_dir = r"./data/validation.csv"
test_data_dir = r"./data/test.npy"

# Models to choose from [resnet, alexnet, vgg, squeezenet, densenet, inception]
model_name = "resnet_adpat"

# Number of classes in the dataset
num_classes = 10

# if you want to see what's in the training set
debug_img = 0

# Batch size for training (change depending on how much memory you have)
batch_size = 256

# Number of epochs to train
num_epochs = 100

# begin_lr
begin_lr = 2e-3

# extra params
ext_params = 'lr_decay-{}-bs-{}-ep-{}' .format(begin_lr, batch_size, num_epochs)

# Flag for feature extracting. When False, we finetune the whole model,
#   when True we only update the reshaped layer params
feature_extract = False

def parse_args():
    """
    Helper function parsing the command line options
    @retval ArgumentParser
    """
    parser = ArgumentParser(description="Training Params")

    parser.add_argument('--dist', 
                        help='whether to use distributed training', default=False, action='store_true')

    return parser.parse_args()

def train_model(model, dataloaders, criterion, optimizer, scheduler=None, num_epochs=15, dist=False):
    since = time.time()

    # validation accuracy
    val_acc_history = []
    train_acc_history = []
    # for save the best accurate model
    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0

    for epoch in tqdm(range(num_epochs), ncols=70):
        print('\n [*] Epoch {}/{}'.format(epoch, num_epochs - 1))

        # Each epoch has a training and validation phase
        # In fact the input has two dataloader(one for train and one for test)
        for phase in ['train', 'val']:
            print(' [**] Begin {} ...'.format(phase))
            if phase == 'train':
                model.train()  # Set model to training mode
            else:
                model.eval()   # Set model to evaluate mode

            sum_loss = torch.tensor(0.)
            sum_metric = torch.tensor(0.)
            num_inst = torch.tensor(0.)

            # Iterate over data.
            # Another way: dataIter = iter(dataloaders[phase]) then next(dataIter)
            for inputs, labels in dataloaders[phase]:
                inputs = inputs.cuda()
                # labels = torch.tensor(labels, dtype=torch.long, device=device)
                labels = labels.squeeze().long().cuda()
                # labels = labels.long()
                # labels = Variable(torch.FloatTensor(inputs.size[0]).uniform_(0, 10).long())

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward
                # track history if only in train
                # notice the validation set will run this with block but do not set gradients trainable
                with torch.set_grad_enabled(phase == 'train'):
                    # Get model outputs and calculate loss
                    # criterion define the loss function
                    # calculate the loss also on the validation set
                    outputs = model(inputs)
                    loss = criterion(outputs, labels)
                    # # along the batch axis
                    # _, preds = torch.max(outputs, 1)

                    # backward + optimize parameters only if in training phase
                    if phase == 'train':
                        loss.backward()
                        optimizer.step()

                # statistics
                sum_loss += loss.item() * inputs.size(0)
                iter_arr = float((outputs.argmax(dim=1) == labels.data).sum().item()) #torch.sum(preds == labels.data)
                sum_metric += iter_arr
                num_inst += outputs.shape[0]

            if dist:
                num_inst = num_inst.clone().cuda()
                sum_metric = sum_metric.clone().cuda()
                sum_loss = sum_loss.clone().cuda()
                distributed.all_reduce(num_inst, op=distributed.ReduceOp.SUM)
                distributed.all_reduce(sum_metric, op=distributed.ReduceOp.SUM)
                distributed.all_reduce(sum_loss, op=distributed.ReduceOp.SUM)
                epoch_acc = (sum_metric / num_inst).detach().cpu().item()
                epoch_loss = (sum_loss / num_inst).detach().cpu().item()
            else:
                epoch_acc = (sum_metric / num_inst).detach().cpu().item()
                epoch_loss = (sum_loss / num_inst).detach().cpu().item()
                
            print('{} Loss: {:.4f} Acc: {:.4f}'.format(phase, epoch_loss, epoch_acc))

            # deep copy the model
            if phase == 'val' and epoch_acc > best_acc:
                best_acc = epoch_acc
                best_model_wts = copy.deepcopy(model.state_dict())
            if phase == 'val':
                val_acc_history.append(epoch_acc)
                lr_decay_metric = epoch_loss
            else:
                train_acc_history.append(epoch_acc)

        if scheduler:
            scheduler.step(lr_decay_metric)

    time_elapsed = time.time() - since
    print(' Training complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))
    print(' Best val Acc: {:4f}'.format(best_acc))

    # load best model weights
    model.load_state_dict(best_model_wts)
    return model, val_acc_history, train_acc_history

def set_parameter_requires_grad(model, feature_extracting):
    """
    When feature extract with pretrained model, we needn't retrain the parameters before FC
    But different when fine tune
    """
    if feature_extracting:
        for param in model.parameters():
            param.requires_grad = False

def initialize_model(model_name, num_classes, feature_extract=True, use_pretrained=True):
    # Initialize these variables which will be set in this if statement. Each of these
    #   variables is model specific.
    # Other wise we will need to define the structure by ourselves with forward function using module and sequential to organize
    
    model_ft = None
    input_size = 0

    if model_name == "resnet":
        """ Resnet52
        """
        model_ft = models.resnet18(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.fc.in_features # (fc): Linear(in_features=2048, out_features=1000, bias=True)
        model_ft.fc = nn.Linear(num_ftrs, num_classes) # replace fc with 2048 to num_class
        input_size = 224

    elif model_name == "alexnet":
        """ Alexnet
        """
        model_ft = models.alexnet(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.classifier[6].in_features
        model_ft.classifier[6] = nn.Linear(num_ftrs,num_classes)
        input_size = 224

    elif model_name == "vgg":
        """ VGG11_bn
        """
        model_ft = models.vgg11_bn(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.classifier[6].in_features
        model_ft.classifier[6] = nn.Linear(num_ftrs,num_classes)
        input_size = 224

    elif model_name == "squeezenet":
        """ Squeezenet
        """
        model_ft = models.squeezenet1_0(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        model_ft.classifier[1] = nn.Conv2d(512, num_classes, kernel_size=(1,1), stride=(1,1))
        model_ft.num_classes = num_classes
        input_size = 224

    elif model_name == "densenet":
        """ Densenet
        """
        model_ft = models.densenet121(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.classifier.in_features
        model_ft.classifier = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == "LeNet":
        input_size = 28
        model_ft = LeNet()

    elif model_name == "resnet_adpat":
        input_size = 28
        model_ft = ResNet18(num_classes)

    else:
        print("Invalid model name, exiting...")
        exit()

    return model_ft, input_size

def inference(model, dataloader):
    print(' Begining testing')
    since = time.time()

    test_result = None
    for inputs, _ in dataloader:
        inputs = inputs.cuda()

        # forward
        with torch.set_grad_enabled(False):
            outputs = model(inputs)
            _, preds = torch.max(outputs, 1)
            if test_result is not None:
                test_result = torch.cat((test_result, preds))
            else:
                test_result = preds

    time_elapsed = time.time() - since
    print(' Testing complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))

    return test_result

if __name__ == "__main__":
    args = parse_args()
    ext_params += '-dist-{}' .format(args.dist)
    # Step1 Model:
    # Initialize the model for this run
    model_ft, input_size = initialize_model(model_name, num_classes, feature_extract, use_pretrained=True)

    if args.dist:
        distributed.init_process_group(backend='nccl', init_method='env://')
        local_rank = int(os.environ.get('LOCAL_RANK') or 0)
        world_size = int(os.environ.get('WORLD_SIZE') or 1)
        torch.cuda.set_device(local_rank)
        model_ft = model_ft.cuda()
        model_ft = DDP(model_ft, device_ids=[local_rank], output_device=local_rank)
        print(" >> Distribute the model")
    else:
        local_rank = int(os.environ.get('LOCAL_RANK') or 0)
        torch.cuda.set_device(local_rank)
        model_ft.cuda()
        
    # Step2 Dataset:
    # Data augmentation and normalization function for training
    # Just normalization for validation
    print(" >> Initializing Datasets and Dataloaders")

    train_data = np.load(train_data_dir)
    train_gt = np.genfromtxt(train_gt_dir, delimiter=',')
    train_gt = train_gt[1:,]
    val_data = np.load(validation_data_dir)
    val_gt = np.genfromtxt(validation_gt_dir, delimiter=',')
    val_gt = val_gt[1:,]
    test_data = np.load(test_data_dir)
    mean = np.mean(test_data.ravel())
    std = np.std(test_data.ravel())
    
    train_tensor_x = torch.stack([torch.Tensor(i) for i in train_data])
    train_tensor_x = train_tensor_x.reshape((-1, 1, 28, 28))

    train_tensor_y = torch.stack([torch.Tensor(np.asarray(i[1])) for i in train_gt])
    train_tensor_y = train_tensor_y.reshape(train_tensor_y.shape[0], 1)

    val_tensor_x = torch.stack([torch.Tensor(i) for i in val_data])
    val_tensor_x = val_tensor_x.reshape((-1, 1, 28, 28))

    val_tensor_y = torch.stack([torch.Tensor(np.asarray(i[1])) for i in val_gt])
    val_tensor_y = val_tensor_y.reshape(val_tensor_y.shape[0], 1)

    test_tensor_x = torch.stack([torch.Tensor(i) for i in test_data])
    test_tensor_x = test_tensor_x.reshape((-1, 1, 28, 28))

    tensor_x={'train':train_tensor_x, 'val':val_tensor_x, 'test':test_tensor_x}
    tensor_y={'train':train_tensor_y, 'val':val_tensor_y, 'test':None}
    
    data_transforms = {
        'train': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.RandomCrop(input_size, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.RandomVerticalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([74.6176043792517/255.0], [85.28552921727005/255.0])
            # transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ]),
        'val': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.CenterCrop(input_size),
            transforms.ToTensor(),
            transforms.Normalize([74.6176043792517/255.0], [85.28552921727005/255.0])
            # transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ]),
        'test': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.ToTensor(),
            transforms.Normalize([mean/255.0], [std/255.0])
        ]),
    }

    # Create valing and validation datasets
    image_datasets_dict = {phase: CustomTensorDataset(tensors=(tensor_x[phase], tensor_y[phase]), 
                                                        transform=data_transforms[phase], 
                                                        showing_img=False,
                                                        is_training=False if phase=='test' else True, 
                                                        clone_to_three=False) for phase in ['train', 'val', 'test']}
    if args.dist:
        datasampler_dict = {phase: DistributedSampler(dataset=image_datasets_dict[phase],
                                                        num_replicas=world_size, 
                                                        rank=local_rank,
                                                        shuffle=True) for phase in ['train', 'val']}
    
        # Create training and validation dataloaders
        dataloaders_dict = {phase: DataLoader(image_datasets_dict[phase], 
                                                batch_size=batch_size,
                                                sampler=datasampler_dict[phase], 
                                                num_workers=4) for phase in ['train', 'val']}
        dataloaders_dict['test'] = DataLoader(image_datasets_dict['test'], 
                                                batch_size=batch_size,
                                                shuffle=False,
                                                num_workers=4)
    else:
        # Create training and validation dataloaders
        dataloaders_dict = {phase: DataLoader(image_datasets_dict[phase], 
                                                batch_size=batch_size,
                                                shuffle=False if phase=='test' else True, 
                                                num_workers=4) for phase in ['train', 'val', 'test']}
    if debug_img:# debug the dataset
        img_dataset_show = CustomTensorDataset(tensors=(tensor_x['test'], tensor_y['test']),
                                                transform=None,
                                                showing_img=True,
                                                is_training=False,
                                                clone_to_three=False)
        img_loader_show = DataLoader(img_dataset_show, batch_size=1)
        # iterate
        for idx, (x, y) in enumerate(img_loader_show):
            if idx > debug_img:
                break

    # Step4 Optimizer
    # Gather the parameters to be optimized/updated in this run. If we are
    #  finetuning we will be updating all parameters. However, if we are
    #  doing feature extract method, we will only update the parameters
    #  that we have just initialized, i.e. the parameters with requires_grad
    #  is True.
    
    if feature_extract:
        params_to_update = []
        for name,param in model_ft.named_parameters():
            if param.requires_grad == True:
                params_to_update.append(param)
                # print("\t",name)
    else:
        params_to_update = model_ft.parameters()
        for name,param in model_ft.named_parameters():
            if param.requires_grad == True:
                pass
                # print("\t",name)
    
    # Observe that all parameters are being optimized
    optimizer_ft = optim.Adam(params_to_update, lr=begin_lr) #optim.SGD(params_to_update, lr=0.001, momentum=0.9)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer_ft,mode='min',factor=0.2,patience=3) 
    #optim.lr_scheduler.StepLR(optimizer_ft, step_size = 20, gamma=0.33)

    # Step5 Loss and train
    # Setup the loss fxn
    criterion = nn.CrossEntropyLoss()
    print(' >> Model Created And Begin Training')
    # Train and evaluate
    model_ft, val_hist, train_hist = \
        train_model(model_ft, dataloaders_dict, criterion, optimizer_ft, scheduler, num_epochs=num_epochs, dist=args.dist)
    if not os.path.exists("./model"):
        os.mkdir("./model")
    torch.save(model_ft.state_dict(), './model/{}-{}-{}.pkl' .format(model_name, date.today(), ext_params)) 
    
    # show training result
    if not args.dist or (args.dist and local_rank == 0):
        plt.figure(1)
        plt.title("Validation Accuracy vs. Number of Training Epochs")
        plt.xlabel("Training Epochs")
        plt.ylabel("Validation Accuracy")
        plt.plot(range(1,num_epochs+1),val_hist,label = "validation")
        plt.plot(range(1,num_epochs+1),train_hist,label = "training")
        # plt.ylim((0.6,1.))
        plt.xticks(np.arange(1, num_epochs+1, 1.0))
        plt.legend()
        plt.savefig('./model/{}-{}-{}.png' .format(model_name, date.today(), ext_params))
        # print(' >> Validation history {}\r\n Training history {}'.format(val_hist, train_hist))

    # model_ft.load_state_dict(torch.load('./model/resnet-2019-11-22.pkl'))
    # model_ft = model_ft.to(device)
    # run test
    
    model_ft.eval()
    test_result = inference(model_ft, dataloaders_dict['test'])
    test_result = test_result.cpu().detach().numpy()
    # test_result = test_result.reshape((test_result.shape[0],1))
    csv_file = np.zeros((test_result.shape[0],2), dtype=np.int32)
    csv_file[:,0] = np.arange(test_result.shape[0])
    csv_file[:,1] = test_result
    if not args.dist or (args.dist and local_rank == 0):
        with open("./data/test-{}-{}-{}.csv".format(model_name, date.today(), ext_params), "wb") as f:
            f.write(b'image_id,label\n')
            np.savetxt(f, csv_file.astype(int), fmt='%i', delimiter=",")
        # print(' >> Save model to "./model/{}-{}-{}.pkl"' .format(model_name, date.today(), ext_params))