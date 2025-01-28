
# Copyright (c) OpenMMLab. All rights reserved.
import os
import cv2
import glob
import torch
import random
from PIL import Image
import numpy as np
from erfnet import ERFNet
#from enet import ENet
#from bisenetv1 import BiSeNetV1
import os.path as osp
from argparse import ArgumentParser
from ood_metrics import fpr_at_95_tpr, calc_metrics, plot_roc, plot_pr,plot_barcode
from sklearn.metrics import roc_auc_score, roc_curve, auc, precision_recall_curve, average_precision_score
import torch.nn.functional as F
from torchvision.transforms import ToTensor,Compose,Resize
seed = 42

# general reproducibility
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)

NUM_CHANNELS = 3
NUM_CLASSES = 20
# gpu training specific
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True

def main():
    parser = ArgumentParser()
    parser.add_argument(
        "--input",
        default="/home/shyam/Mask2Former/unk-eval/RoadObsticle21/images/*.webp",
        help="A list of space separated input images; "
        "or a single glob pattern such as 'directory/*.jpg'",
    )  
    parser.add_argument('--method', default="MSP", help="Method to use: MSP, MaxLogit, MaxEntropy, etc.")  # New parameter
    parser.add_argument('--loadDir',default="../trained_models/")
    parser.add_argument('--loadWeights', default="erfnet_pretrained.pth")
    parser.add_argument('--loadModel', default="erfnet.py")
    parser.add_argument('--subset', default="val")  #can be val or train (must have labels)
    parser.add_argument('--datadir', default="/home/shyam/ViT-Adapter/segmentation/data/cityscapes/")
    parser.add_argument('--num-workers', type=int, default=4)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--temperature', type=float,default=0)
    
    args = parser.parse_args()
    anomaly_score_list = []
    ood_gts_list = []
    
    # Dimensioni target per immagini e maschere
    size = (512, 1024)  # Cambia se necessario

    # Trasformazioni per immagini e maschere
    input_transform = Compose([
        Resize(size, Image.BILINEAR),  # Ridimensiona immagini
        ToTensor()  # Converte immagini in Tensor
    ])

    mask_transform = Compose([
        Resize(size, Image.NEAREST)  # Ridimensiona maschere
    ])

    if not os.path.exists('results.txt'):
        open('results.txt', 'w').close()
    file = open('results.txt', 'a')

    modelpath = args.loadDir + args.loadModel
    weightspath = args.loadDir + args.loadWeights

    print ("Loading model: " + modelpath)
    print ("Loading weights: " + weightspath)
    print(f"Using method: {args.method}")  # Log the method

    if args.loadModel == 'enet.py':
      model = ENet(NUM_CLASSES)
    elif args.loadModel == 'bisenetv1.py':
      model = BiSeNetV1(NUM_CLASSES)
    elif args.loadModel == 'erfnet.py':
      model = ERFNet(NUM_CLASSES)

    if (not args.cpu):
        model = torch.nn.DataParallel(model).cuda()

    def load_my_state_dict(model, state_dict):  #custom function to load model when not all dict elements
        own_state = model.state_dict()
        for name, param in state_dict.items():
            if name not in own_state:
                if name.startswith("module."):
                    own_state[name.split("module.")[-1]].copy_(param)
                else:
                    print(name, " not loaded")
                    continue
            else:
                own_state[name].copy_(param)
        return model

    model = load_my_state_dict(model, torch.load(weightspath, map_location=lambda storage, loc: storage))
    print ("Model and weights LOADED successfully")
    model.eval()
    
    temperature = args.temperature
    
    for path in glob.glob(os.path.expanduser(str(args.input))):
        print(path)
        images = Image.open(path).convert('RGB')
        images = input_transform(images).unsqueeze(0).float()
        # Aggiungi questo codice per verificare le dimensioni dell'immagine prima del passaggio nel modello
        print("Dimensione dell'immagine prima del passaggio al modello:", images.shape)
        
        with torch.no_grad():
            result = model(images).squeeze(0)
            if temperature != 0:
                scaled_logits = result / temperature
                softmax= F.softmax(scaled_logits,dim=1)
                anomaly_result= 1.0 - torch.max(softmax,dim=1).values.squeeze(0)
                anomaly_result=anomaly_result.cpu().numpy()
            else:
                if args.method == "MSP":
                    softmax = F.softmax(result, dim=1)
                    anomaly_result = 1.0 - torch.max(softmax, dim=1).values.squeeze(0)  
                    anomaly_result = anomaly_result.cpu().numpy() 
                elif args.method == "MaxLogit":
                    anomaly_result = -torch.max(result, dim=1).values.squeeze(0)  
                    anomaly_result = anomaly_result.cpu().numpy() 
                elif args.method == "MaxEntropy":
                    probs = F.softmax(result,dim=1)
                    entropy= -torch.sum(probs*torch.log(probs+ 1e-10),dim=1) 
                    anomaly_result = entropy.data.cpu().numpy().astype("float32")
                elif args.method == "Void" :
                    anomaly_result = F.softmax(result, dim=0)[-1]
                    anomaly_result = anomaly_result.data.cpu().numpy()
                else:
                    raise ValueError(f"Unknown method: {args.method}")
                      
        pathGT = path.replace("images", "labels_masks")                
        if "RoadObsticle21" in pathGT:
           pathGT = pathGT.replace("webp", "png")
        if "fs_static" in pathGT:
           pathGT = pathGT.replace("jpg", "png")                
        if "RoadAnomaly" in pathGT:
           pathGT = pathGT.replace("jpg", "png")  

        mask = Image.open(pathGT)
        mask = mask_transform(mask)
        ood_gts = np.array(mask)

        if "RoadAnomaly" in pathGT:
            ood_gts = np.where((ood_gts==2), 1, ood_gts)
        if "LostAndFound" in pathGT:
            ood_gts = np.where((ood_gts==0), 255, ood_gts)
            ood_gts = np.where((ood_gts==1), 0, ood_gts)
            ood_gts = np.where((ood_gts>1)&(ood_gts<201), 1, ood_gts)

        if "Streethazard" in pathGT:
            ood_gts = np.where((ood_gts==14), 255, ood_gts)
            ood_gts = np.where((ood_gts<20), 0, ood_gts)
            ood_gts = np.where((ood_gts==255), 1, ood_gts)

        if 1 not in np.unique(ood_gts):
            continue              
        else:
             ood_gts_list.append(ood_gts)
             anomaly_score_list.append(anomaly_result)
        del result, anomaly_result, ood_gts, mask
        if 'softmax_probs' in locals():
            del softmax_probs

        torch.cuda.empty_cache()

    file.write( "\n")

    ood_gts = np.array(ood_gts_list)
    anomaly_scores = np.array(anomaly_score_list)
    print(type(anomaly_scores))
    print(anomaly_scores.shape)

    ##
    # 1. Ottenere il batch_size dinamicamente
    batch_size = anomaly_scores.shape[0]  # per ottenere la dimensione del batch
    # 2. Ridimensionare anomaly_scores nella forma (batch_size, 512, 1024)
    anomaly_scores = anomaly_scores.reshape(batch_size, 512, 1024)

    ood_mask = (ood_gts == 1)
    ind_mask = (ood_gts == 0)

    print(f"Shape of anomaly_scores: {anomaly_scores.shape}")
    print(f"Shape of ood_mask: {ood_mask.shape}")

    ood_out = anomaly_scores[ood_mask]
    ind_out = anomaly_scores[ind_mask]

    ood_label = np.ones(len(ood_out))
    ind_label = np.zeros(len(ind_out))
    
    val_out = np.concatenate((ind_out, ood_out))
    val_label = np.concatenate((ind_label, ood_label))

    prc_auc = average_precision_score(val_label, val_out)
    fpr = fpr_at_95_tpr(val_out, val_label)

    print(f'AUPRC score: {prc_auc*100.0}')
    print(f'FPR@TPR95: {fpr*100.0}')

    file.write(('AUPRC score:' + str(prc_auc*100.0) + '   FPR@TPR95:' + str(fpr*100.0) ))
    file.close()

if __name__ == '__main__':
    main()
