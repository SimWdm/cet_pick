from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import json
import cv2
import numpy as np
import time
from progress.bar import Bar
import torch
from opts import opts
from logger import Logger
from utils.utils import AverageMeter
# from utils.loader import load_subtomo,load_subtomo_v2
from utils.loader import load_rec
from cet_pick.datasets.dataset_factory import dataset_factory
from cet_pick.detectors.detector_factory import detector_factory
from utils.image import gaussian_radius, draw_umich_gaussian_3d, draw_msra_gaussian_3d, flip_ud, flip_lr, CornerErasing, CenterOut
import torchio as tio 
from cet_pick.utils.loader import load_tomos_from_list, cutup
from cet_pick.models.model import create_model, load_model 
from cet_pick.utils.memory_bank import MemoryBank
from sklearn.cluster import KMeans
import mrcfile
import torchvision.transforms as T
from cet_pick.utils.lie_tools import random_SO3, constrained_SO3
from cet_pick.utils.project3d import Projector
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

class PrefetchDatasetProj(torch.utils.data.Dataset):

    def __init__(self, opt, dataset):
        self.opt = opt 
        self.tomos = dataset.tomos 
        self.subvols = dataset.subvols
        self.subvols_3d = dataset.sub_vols_3d
        self.mean_subvols = dataset.mean_subvols
        self.mean_subvols3d = dataset.mean_subvols3d 
        self.std_subvols3d = dataset.std_subvols3d
        self.std_subvols = dataset.std_subvols
        self.names = dataset.names_all
        self.coords = dataset.coords
        self.size = dataset.size
        self.transforms = T.Compose([
            T.ToPILImage(),
            T.CenterCrop(self.size[1]),
            T.ToTensor(),
            T.Normalize((self.mean_subvols, self.mean_subvols3d),(self.std_subvols, self.std_subvols3d))]
            # T.Normalize((0.5),(0.5))]
            )

    def __getitem__(self, index): 

        coord = self.coords[index]
        name = self.names[index]

        subtomo = self.subvols[index]
        subtomo_3d = self.subvols_3d[index]
        subtomo_all = torch.cat([subtomo, subtomo_3d], dim=0)

        normed_tomo = self.transforms(subtomo_all)

        ret = {'input':normed_tomo[0:1].float(), 'input_3d': normed_tomo[1:2].float(),'coord': coord, 'label': 0, 'name': name, 'orig': subtomo, 'orig_3d':subtomo_3d, 'orig_all': subtomo_all}

        return ret 

    def __len__(self):
        return len(self.subvols)


class PrefetchDataset2D(torch.utils.data.Dataset):

    def __init__(self, opt, dataset):
        self.opt = opt 
        self.tomos = dataset.tomos 
        self.subvols = dataset.subvols
        self.labels = dataset.labels
        self.size = dataset.size
        self.transforms = T.Compose([
            T.ToPILImage(),
            T.CenterCrop(self.size[1]-self.size[1]//4),
            T.ToTensor(),
            T.Normalize((0.5),(0.5))]
            )
        rot = torch.tensor([[1,0,0],[0,1,0],[0,0,1]])
        self.rot = rot.unsqueeze(0).float()


    def __getitem__(self, index): 

        label = self.labels[index]
        subtomo = self.subvols[index]
        zz = self.size[0]//2 
        sub_vol_c = np.zeros(subtomo.shape)
        sub_vol_c[zz-4:zz+4] = subtomo[zz-4:zz+4]
        projector = Projector(sub_vol_c)
        proj = projector.project(self.rot)
        normed_tomo = self.transforms(proj)

        ret = {'input':normed_tomo.float(), 'label': label}

        return ret 

    def __len__(self):
        return len(self.subvols)


class PrefetchDataset(torch.utils.data.Dataset):

    def __init__(self, opt, dataset):
        self.opt = opt 
        # self.images = dataset.images 
        # self.images = dataset.tomos
        # self.image_paths = dataset.subtomo_paths 
        self.tomos = dataset.tomos 
        self.subvols = dataset.subvols
        self.labels = dataset.labels
        self.size = dataset.size
    

    def __getitem__(self, index): 
        norm = tio.Compose([
            tio.transforms.Crop((self.size[0]//8, self.size[1]//8, self.size[2]//8)),
            tio.transforms.ZNormalization(),
            tio.transforms.RescaleIntensity(out_min_max=(-3,3)),
            tio.transforms.ZNormalization()
                ]
            )

        label = self.labels[index]
        # subtomo = load_subtomo_v2(subtomo_path)
        subtomo = self.subvols[index]

        subtomo = np.expand_dims(subtomo,axis=0).copy()

        normed_tomo = norm(subtomo)

        ret = {'input':normed_tomo, 'label': label}

        return ret 

    def __len__(self):
        return len(self.subvols)

        


def test(opt):
    os.environ['CUDA_VISIBLE_DEVICES'] = opt.gpus_str
    Dataset = dataset_factory[opt.dataset]
    opt = opts().update_dataset_info_and_set_heads(opt, Dataset)
    print(opt)
    Logger(opt)
    opt.device = torch.device('cuda' if opt.gpus[0] >= 0 else 'cpu')
    model = create_model(opt.arch, opt.heads, opt.head_conv)
    model = load_model(model, opt.load_model)
    model = model.to(opt.device)
    model.eval()
    split = 'test'

    dataset = Dataset(opt, split,(3,opt.bbox,opt.bbox),sigma1=opt.dog)
    data_loader = torch.utils.data.DataLoader(PrefetchDatasetProj(opt, dataset), batch_size=256, shuffle=False, num_workers=1, pin_memory=True)
    num_iters = len(dataset)//opt.batch_size
    # memory_bank_test = MemoryBank(len(dataset), 256, 2, 0.07)
    bar = Bar('{}'.format(opt.exp_id), max=num_iters)
    time_stats = ['tot_time', 'load', 'pre', 'net', 'dec']
    avg_time_stats = {t: AverageMeter() for t in time_stats}
    all_proj_vecs = []
    all_coords = []
    all_pred_vecs = []
    all_subvols = []
    all_subvols_3d = []
    all_names = []
    all_orig_subvols = []
    all_origs = []
    for iter_id, batch in enumerate(data_loader):
        input_tomo = batch['input'].to(opt.device)
        input_tomo_3d = batch['input_3d'].to(opt.device)
        coord = batch['coord']
        name = batch['name']
        label_tomo = batch['label']
        orig_tomo = batch['orig']
        orig_tomo_3d = batch['orig_3d']
        orig_tomo_all = batch['orig_all']
        ret = model.forward_test(input_tomo, input_tomo_3d)
        proj = ret['proj'].detach().cpu().numpy()
        pred = ret['pred'].detach().cpu().numpy()
        all_pred_vecs.append(pred)
        all_proj_vecs.append(proj)
        all_subvols.append(input_tomo.detach().cpu().numpy())
        all_subvols_3d.append(input_tomo_3d.detach().cpu().numpy())
        all_coords.append(coord)
        all_names.append(name)

    Bar.suffix = '[{0}/{1}]|Tot: {total:} |ETA: {eta:} '.format(
                   iter_id, num_iters, total=bar.elapsed_td, eta=bar.eta_td)

    bar.next()
    bar.finish()
    topk = 10
    all_proj_vecs = np.concatenate(all_proj_vecs, axis = 0)
    all_pred_vecs = np.concatenate(all_pred_vecs, axis =0 )
    all_coords = np.concatenate(all_coords, axis=0)
    all_names = np.concatenate(all_names, axis=0)
    # all_origs = np.concatenate(all_origs, axis=0)

    all_subvols = np.concatenate(all_subvols, axis=0)

    all_subvols_3d = np.concatenate(all_subvols_3d, axis=0)
    
    


    print('opt.save_dir', opt.save_dir)
    dataset_number = opt.exp_id.split('_')[0]
    # # # # all_feature_maps = np.concatenate(feature_maps, axis=0)
    # save_proj = os.path.join(opt.save_dir, 'preselect_{}_proj_f.npy'.format(dataset_number))
    # # save_lb = os.path.join(opt.save_dir, 'shrec_labels_box32_withgold.npy')
    # save_name = os.path.join(opt.save_dir, 'preselect_{}_names_f.npy'.format(dataset_number))
    # save_pred = os.path.join(opt.save_dir, 'preselect_{}_pred_f.npy'.format(dataset_number))
    # save_subvols = os.path.join(opt.save_dir, 'preselect_{}_subvol_f.npy'.format(dataset_number))
    # save_indices = os.path.join(opt.save_dir, 'preselect_{}_ind_f.npy'.format(dataset_number))
    # save_coords = os.path.join(opt.save_dir, 'preselect_{}_coord_f.npy'.format(dataset_number))
    # save_origsub = os.path.join(opt.save_dir, 'preselect_{}_origsub_f.npy'.format(dataset_number))
    # save_origsub_3d = os.path.join(opt.save_dir, 'preselect_{}_origsub3d_f.npy'.format(dataset_number))
    # save_origsub_all = os.path.join(opt.save_dir, 'preselect_{}_origsub_all_f.npy'.format(dataset_number))
    out_file = os.path.join(opt.save_dir, 'all_output_info.npz')
    pa_table = {"proj": all_proj_vecs, "pred": all_pred_vecs, "name": all_names, "coords": all_coords, "subvol": all_subvols_3d, "subvols_2d": all_subvols}
    np.savez(out_file, **pa_table)
    # # np.save('test_proj_vecs_box36.npy', all_proj_vecs)
    # # np.save('test_labels_box36.npy', all_labels)
    # # np.save('test_pred_vecs_box36.npy', all_pred_vecs)
    # np.save(save_name, all_names)
    # np.save(save_indices, indices)
    # np.save(save_subvols, all_subvols)
    # np.save(save_pred, all_pred_vecs)
    # np.save(save_proj, all_proj_vecs)
    # np.save(save_coords, all_coords)
    # np.save(save_origsub, all_orig_subvols)
    # np.save(save_origsub_3d, all_subvols_3d)
    # np.save(save_origsub_all, all_origs)
    # np.save(save_lb, all_labels)

if __name__ == '__main__':
    opt = opts().parse()
    test(opt)

