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
from ResNet import ResNet18, ResNet34
from optimization import WarmupLinearSchedule, WarmupCosineSchedule, AdamW
from MyTransforms import RandomPepperNoise

print("PyTorch Version: ",torch.__version__)

# hyperparameter
# Top level data directory. Here we assume the format of the directory conforms to the ImageFolder structure

train_data_dir = r"./data/train.npy"
train_gt_dir = r"./data/train.csv"
validation_data_dir = r"./data/validation.npy"
validation_gt_dir = r"./data/validation.csv"
test_data_dir = r"./data/test.npy"
src_data_dir = r'./data/src.npy'
src_gt_dir = r'./data/src.csv'

# Models to choose from [resnet, alexnet, vgg, squeezenet, densenet, inception]
model_name = "resnet_adpat"

# Number of classes in the dataset
num_classes = 10

# if you want to see what's in the training set
debug_img = 0

# Batch size for training (change depending on how much memory you have)
batch_size = 64

# folds
k_folds = 10

# Number of epochs to train
num_epochs = 100

# begin_lr
begin_lr = 4e-2

# lr_schedule
lr_schedule = 'cosine'  #plateau

# optimizer
optim_type = 'AdamW'

# extra params
ext_params = 'lr_decay-{}-bs-{}-ep-{}-folds-{}-lrs-{}-optim-{}' \
            .format(begin_lr, batch_size, num_epochs, k_folds, lr_schedule, optim_type)

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
            for nbatch, (inputs, labels) in enumerate(dataloaders[phase]):
                global_steps = len(dataloaders[phase]) * epoch + nbatch
                os.environ['global_steps'] = str(global_steps)
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
                        if lr_schedule == 'triangle' or lr_schedule == 'cosine':
                            scheduler.step()
                        optimizer.step()
                        # for param_group in optimizer.param_groups:
                        #     print('{} : {}'.format(len(dataloaders[phase]) * epoch + nbatch, param_group['lr']))

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

        if lr_schedule == 'plateau':
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

    elif model_name == "resnet_adpat34":
        input_size = 28
        model_ft = ResNet34(num_classes)

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
            if test_result is not None:
                test_result = torch.cat((test_result, outputs))
            else:
                test_result = outputs

    time_elapsed = time.time() - since
    print(' Testing complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))

    return test_result

def dataloaders_dict_create(data_transforms, train_data, train_gt, val_data, val_gt, dist=True, local_rank=0, world_size=1):
    train_tensor_x = torch.stack([torch.Tensor(i) for i in train_data])
    train_tensor_x = train_tensor_x.reshape((-1, 1, 28, 28))

    train_tensor_y = torch.stack([torch.Tensor(np.asarray(i[1])) for i in train_gt])
    train_tensor_y = train_tensor_y.reshape(train_tensor_y.shape[0], 1)

    val_tensor_x = torch.stack([torch.Tensor(i) for i in val_data])
    val_tensor_x = val_tensor_x.reshape((-1, 1, 28, 28))

    val_tensor_y = torch.stack([torch.Tensor(np.asarray(i[1])) for i in val_gt])
    val_tensor_y = val_tensor_y.reshape(val_tensor_y.shape[0], 1)

    tensor_x={'train':train_tensor_x, 'val':val_tensor_x}
    tensor_y={'train':train_tensor_y, 'val':val_tensor_y}
    
    # Create valing and validation datasets
    image_datasets_dict = {phase: CustomTensorDataset(tensors=(tensor_x[phase], tensor_y[phase]), 
                                                        transform=data_transforms[phase], 
                                                        showing_img=False,
                                                        is_training=True, 
                                                        clone_to_three=False) for phase in ['train', 'val']}
    if dist:
        datasamplers_dict = {phase: DistributedSampler(dataset=image_datasets_dict[phase],
                                                        num_replicas=world_size, 
                                                        rank=local_rank,
                                                        shuffle=True) for phase in ['train', 'val']}
    
        # Create training and validation dataloaders
        dataloaders_dict = {phase: DataLoader(image_datasets_dict[phase], 
                                                batch_size=batch_size,
                                                sampler=datasamplers_dict[phase], 
                                                num_workers=4) for phase in ['train', 'val']}

    else:
        # Create training and validation dataloaders
        dataloaders_dict = {phase: DataLoader(image_datasets_dict[phase], 
                                                batch_size=batch_size,
                                                shuffle=True, 
                                                num_workers=4) for phase in ['train', 'val']}
    return dataloaders_dict


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
    else:
        world_size = 1
        local_rank = int(os.environ.get('LOCAL_RANK') or 0)
        torch.cuda.set_device(local_rank)

    print(" >> Initializing Datasets and Dataloaders")
    all_data = np.load(src_data_dir)
    all_gt = np.genfromtxt(src_gt_dir, delimiter=',')
    all_gt = all_gt[1:,]
    train_mean = np.mean(all_data.ravel())
    train_std = np.std(all_data.ravel())
    test_data = np.load(test_data_dir)
    test_mean = np.mean(test_data.ravel())
    test_std = np.std(test_data.ravel())
    data_transforms = {
        'train': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.RandomCrop(input_size, padding=4),
            transforms.RandomRotation(15, resample=False, expand=False),
            transforms.RandomHorizontalFlip(),
            transforms.RandomVerticalFlip(),
            # RandomPepperNoise(snr=0.99,p=0.5),
            transforms.ToTensor(),
            transforms.Normalize([train_mean/255.0], [train_std/255.0])
            # transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ]),
        'val': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.CenterCrop(input_size),
            transforms.ToTensor(),
            transforms.Normalize([train_mean/255.0], [train_std/255.0])
            # transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ]),
        'test': transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(input_size, interpolation=Image.NEAREST),
            transforms.ToTensor(),
            transforms.Normalize([test_mean/255.0], [test_std/255.0])
        ]),
    }

    if k_folds == 0:
        train_data = np.load(train_data_dir)
        train_gt = np.genfromtxt(train_gt_dir, delimiter=',')
        train_gt = train_gt[1:,]
        val_data = np.load(validation_data_dir)
        val_gt = np.genfromtxt(validation_gt_dir, delimiter=',')
        val_gt = val_gt[1:,]
        
        dataloaders_dict = dataloaders_dict_create(data_transforms, train_data, train_gt, val_data, val_gt, args.dist, local_rank, world_size)

    else:
        all_length = len(all_data)
        all_idx = np.arange(all_length)
        np.random.shuffle(all_idx)
        dataloaders_dict_list = []
        for fold in range(k_folds):
            b_idx = int(all_length/k_folds*fold)
            e_idx = int(all_length/k_folds*(fold+1))
            train_idx = np.concatenate((all_idx[:b_idx], all_idx[e_idx:]), 0)
            val_idx = all_idx[b_idx:e_idx]
            dataloaders_dict = dataloaders_dict_create(data_transforms, all_data[train_idx], all_gt[train_idx], 
                                                    all_data[val_idx], all_gt[val_idx], args.dist, local_rank, world_size)
            dataloaders_dict_list.append(dataloaders_dict)
    
    test_tensor_x = torch.stack([torch.Tensor(i) for i in test_data])
    test_tensor_x = test_tensor_x.reshape((-1, 1, 28, 28))

    test_dataset = CustomTensorDataset(tensors=(test_tensor_x, None), 
                                        transform=data_transforms['test'], 
                                        showing_img=False,
                                        is_training=False, 
                                        clone_to_three=False)
    test_dataloader = DataLoader(test_dataset, 
                                batch_size=batch_size,
                                shuffle=False,
                                num_workers=4)

    if debug_img:# debug the dataset
        img_dataset_show = CustomTensorDataset(tensors=(test_tensor_x, None),
                                                transform=None,
                                                showing_img=True,
                                                is_training=False,
                                                clone_to_three=False)
        img_loader_show = DataLoader(img_dataset_show, batch_size=1)
        # iterate
        for idx, (x, y) in enumerate(img_loader_show):
            if idx > debug_img:
                break

    # if feature_extract:
    #     params_to_update = []
    #     for name,param in model_ft.named_parameters():
    #         if param.requires_grad == True:
    #             params_to_update.append(param)
    #             # print("\t",name)
    # else:
    #     params_to_update = model_ft.parameters()
    #     for name,param in model_ft.named_parameters():
    #         if param.requires_grad == True:
    #             pass
    #             # print("\t",name)
    
    if k_folds == 0:
        if args.dist:
            model_ft = model_ft.cuda()
            model_ft = DDP(model_ft, device_ids=[local_rank], output_device=local_rank)
            print(" >> Distribute the model")
        else:
            model_ft.cuda()

        params_to_update = model_ft.parameters()
        # Observe that all parameters are being optimized
        if optim_type == 'Adam':
            optimizer_ft = optim.Adam(params_to_update, lr=begin_lr)
        if optim_type == 'SGD':
            optimizer_ft = optim.SGD(params_to_update, lr=begin_lr, momentum=0.9)
        else:
            optimizer_ft = AdamW(params_to_update, lr=begin_lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=1e-2)

        if lr_schedule == 'plateau':
            # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer_ft,
            #                                                         mode='min',
            #                                                         factor=0.2,
            #                                                         patience=1,
            #                                                         verbose=False,
            #                                                         threshold=1e-4,
            #                                                         threshold_mode='rel',
            #                                                         cooldown=2,
            #                                                         min_lr=0,
            #                                                         eps=1e-8)
            scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer_ft,mode='min',factor=0.2,patience=3)
        if lr_schedule == 'cosine':
            scheduler = WarmupCosineSchedule(optimizer_ft,
                                            warmup_steps=2000,
                                            t_total=int(num_epochs * len(dataloaders_dict['train'])),
                                            cycles=1., 
                                            last_lr=1e-4,
                                            last_epoch = -1)
        else:
            scheduler = WarmupLinearSchedule(optimizer_ft,
                                            warmup_steps=2000,
                                            t_total=int(num_epochs * len(dataloaders_dict['train'])),
                                            last_epoch = -1)
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
        
        model_ft.eval()
        test_result = inference(model_ft, test_dataloader)
        _, preds = torch.max(test_result, 1)

    else:
        avr_val_acc = 0.0
        avr_train_acc = 0.0
        all_test = None
        for fold in range(k_folds):
            # Train and evaluate
            model_ft, input_size = initialize_model(model_name, num_classes, feature_extract, use_pretrained=True)

            if args.dist:
                model_ft = model_ft.cuda()
                model_ft = DDP(model_ft, device_ids=[local_rank], output_device=local_rank)
                print(" >> Distribute the model")
            else:
                model_ft.cuda()

            params_to_update = model_ft.parameters()
            if optim_type == 'Adam':
                optimizer_ft = optim.Adam(params_to_update, lr=begin_lr)
            if optim_type == 'SGD':
                optimizer_ft = optim.SGD(params_to_update, lr=begin_lr, momentum=0.9)
            else:
                optimizer_ft = AdamW(params_to_update, lr=begin_lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=1e-2)
            if lr_schedule == 'plateau':
                # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer_ft,
                #                                                         mode='min',
                #                                                         factor=0.2,
                #                                                         patience=1,
                #                                                         verbose=False,
                #                                                         threshold=1e-4,
                #                                                         threshold_mode='rel',
                #                                                         cooldown=2,
                #                                                         min_lr=0,
                #                                                         eps=1e-8)
                scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer_ft,mode='min',factor=0.2,patience=3) 
            if lr_schedule == 'cosine':
                scheduler = WarmupCosineSchedule(optimizer_ft,
                                                warmup_steps=2000,
                                                t_total=int(num_epochs * len(dataloaders_dict['train'])),
                                                cycles=1., 
                                                last_lr=0.,
                                                last_epoch = -1)
            else:
                scheduler = WarmupLinearSchedule(optimizer_ft,
                                                warmup_steps=2000,
                                                t_total=int(num_epochs * len(dataloaders_dict['train'])),
                                                last_epoch = -1)
            criterion = nn.CrossEntropyLoss()
            print(' >> Model Created And Begin Training for folds-{}'.format(fold))
            model_ft, val_hist, train_hist = \
                train_model(model_ft, dataloaders_dict, criterion, optimizer_ft, scheduler, num_epochs=num_epochs, dist=args.dist)
            
            if not args.dist or (args.dist and local_rank == 0):
                if not os.path.exists("./model/{}-{}-{}".format(model_name, date.today(), ext_params)):
                    os.mkdir("./model/{}-{}-{}".format(model_name, date.today(), ext_params))
                torch.save(model_ft.state_dict(), './model/{}-{}-{}/models-fold-{}.pkl' .format(model_name, date.today(), ext_params, fold)) 
            
            model_ft.eval()
            test_result = inference(model_ft, test_dataloader)
            test_result = nn.functional.softmax(test_result, dim=1)
            if all_test is not None:
                all_test = torch.add(all_test, test_result)
            else:
                all_test = test_result
            avr_val_acc += max(val_hist)
            avr_train_acc += max(train_hist)
            print(' >> Current average training accuracy: {} validation accuracy: {}' .format(avr_train_acc/(fold+1), avr_val_acc/(fold+1)))
        _, preds = torch.max(all_test, 1)
        print(' >> Final average training accuracy: {} validation accuracy: {}' .format(avr_train_acc/k_folds, avr_val_acc/k_folds))
    
    # model_ft.load_state_dict(torch.load('./model/resnet-2019-11-22.pkl'))
    # model_ft = model_ft.to(device)    
    preds = preds.cpu().detach().numpy()
    # test_result = test_result.reshape((test_result.shape[0],1))
    csv_file = np.zeros((preds.shape[0],2), dtype=np.int32)
    csv_file[:,0] = np.arange(preds.shape[0])
    csv_file[:,1] = preds
    if not args.dist or (args.dist and local_rank == 0):
        with open("./data/test-{}-{}-{}.csv".format(model_name, date.today(), ext_params), "wb") as f:
            f.write(b'image_id,label\n')
            np.savetxt(f, csv_file.astype(int), fmt='%i', delimiter=",")
        # print(' >> Save model to "./model/{}-{}-{}.pkl"' .format(model_name, date.today(), ext_params))