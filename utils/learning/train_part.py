import torch
from torch.nn import functional as F
import cv2
import os
import numpy as np
import shutil
import pytorch_lightning as pl
import faiss
from sklearn.random_projection import SparseRandomProjection
from utils.sampling_methods.kcenter_greedy import kCenterGreedy
from scipy.ndimage import gaussian_filter
from sklearn.metrics import roc_auc_score
from sklearn.metrics import confusion_matrix

from utils.common.visualize import visualize_TSNE
from utils.data.load_data import MVTecDataset
from utils.data.transforms import Transform, GT_Transform, INV_Normalize

def min_max_norm(image):
    a_min, a_max = image.min(), image.max()
    return (image - a_min)/(a_max - a_min) 

def cvt2heatmap(gray):
    heatmap = cv2.applyColorMap(np.uint8(gray), cv2.COLORMAP_JET)
    return heatmap

def heatmap_on_image(heatmap, image):
    if heatmap.shape != image.shape:
        heatmap = cv2.resize(heatmap, (image.shape[0], image.shape[1]))
    out = np.float32(heatmap)/255 + np.float32(image)/255
    out = out / np.max(out)
    return np.uint8(255 * out)

def copy_files(src, dst, ignores=[]):
    src_files = os.listdir(src)
    for file_name in src_files:
        ignore_check = [True for i in ignores if i in file_name]
        if ignore_check:
            continue
        full_file_name = os.path.join(src, file_name)
        if os.path.isfile(full_file_name):
            shutil.copy(full_file_name, os.path.join(dst,file_name))
        if os.path.isdir(full_file_name):
            os.makedirs(os.path.join(dst, file_name), exist_ok=True)
            copy_files(full_file_name, os.path.join(dst, file_name), ignores)

def embedding_concat(x, y):
    # from https://github.com/xiahaifeng1995/PaDiM-Anomaly-Detection-Localization-master
    B, C1, H1, W1 = x.size()
    _, C2, H2, W2 = y.size()
    s = int(H1 / H2)
    x = F.unfold(x, kernel_size=s, dilation=1, stride=s)
    x = x.view(B, C1, -1, H2, W2)
    z = torch.zeros(B, C1 + C2, x.size(2), H2, W2)
    for i in range(x.size(2)):
        z[:, :, i, :, :] = torch.cat((x[:, :, i, :, :], y), 1)
    z = z.view(B, -1, H2 * W2)
    z = F.fold(z, kernel_size=s, output_size=(H1, W1), stride=s)

    return z

def reshape_embedding(embedding):
    embedding_list = []
    for k in range(embedding.shape[0]):
        for i in range(embedding.shape[2]):
            for j in range(embedding.shape[3]):
                embedding_list.append(embedding[k, :, i, j])
    return embedding_list

def prep_dirs(root, category):
    # make embeddings dir
    # embeddings_path = os.path.join(root, 'embeddings')
    embeddings_path = os.path.join('./', 'embeddings', category)
    os.makedirs(embeddings_path, exist_ok=True)
    # make sample dir
    sample_path = os.path.join(root, 'sample')
    os.makedirs(sample_path, exist_ok=True)
    # make source code record dir & copy
    source_code_save_path = os.path.join(root, 'src')
    os.makedirs(source_code_save_path, exist_ok=True)
    copy_files('./', source_code_save_path, ['.git','.vscode','__pycache__','logs','README','samples','LICENSE', 'embeddings', 'result']) # copy source code
    return embeddings_path, sample_path, source_code_save_path

def cal_confusion_matrix(y_true, y_pred_no_thresh, thresh, img_path_list):
    pred_thresh = []
    false_n = []
    false_p = []
    for i in range(len(y_pred_no_thresh)):
        if y_pred_no_thresh[i] > thresh:
            pred_thresh.append(1)
            if y_true[i] == 0:
                false_p.append(img_path_list[i])
        else:
            pred_thresh.append(0)
            if y_true[i] == 1:
                false_n.append(img_path_list[i])

    cm = confusion_matrix(y_true, pred_thresh)
    print(cm)
    print('false positive')
    print(false_p)
    print('false negative')
    print(false_n)


class STPM(pl.LightningModule):
    def __init__(self, args):
        super(STPM, self).__init__()

        self.save_hyperparameters(args)
        self.args = args

        self.init_features()
        def hook_t(module, input, output):
            self.features.append(output)
            
        if args.model == 'R152' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet152', pretrained=True)
        elif args.model == 'R101' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet101', pretrained=True)
        elif args.model == 'R18' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=True)
        elif args.model == 'R34' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet34', pretrained=True)
        elif args.model == 'R50' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet50', pretrained=True)
        elif args.model == 'WR50' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'wide_resnet50_2', pretrained=True)
        elif args.model == 'WR101' :
            self.model = torch.hub.load('pytorch/vision:v0.10.0', 'wide_resnet101_2', pretrained=True)

        for param in self.model.parameters():
            param.requires_grad = False

        if args.block_index == '1+2' :
            self.model.layer1[-1].register_forward_hook(hook_t)
            self.model.layer2[-1].register_forward_hook(hook_t)
        elif args.block_index == '2+3' :
            self.model.layer2[-1].register_forward_hook(hook_t)
            self.model.layer3[-1].register_forward_hook(hook_t)
        elif args.block_index == '3+4' :
            self.model.layer3[-1].register_forward_hook(hook_t)
            self.model.layer4[-1].register_forward_hook(hook_t)
        elif args.block_index == '5' :
            self.model.avgpool.register_forward_hook(hook_t)
        elif args.block_index == '4' :
            self.model.layer4[-1].register_forward_hook(hook_t)

        self.criterion = torch.nn.MSELoss(reduction='sum')

        self.init_results_list()

        self.inv_normalize = INV_Normalize()
        
        self.viz_feature_list = []
        self.viz_class_idx_list = []

    def init_results_list(self):
        self.gt_list_px_lvl = []
        self.pred_list_px_lvl = []
        self.gt_list_img_lvl = []
        self.pred_list_img_lvl = []
        self.img_path_list = []
        self.img_type_list = []

    def init_features(self):
        self.features = []

    def forward(self, x_t):
        self.init_features()
        _ = self.model(x_t)
        return self.features

    def save_anomaly_map(self, anomaly_map, input_img, gt_img, file_name, x_type):
        if anomaly_map.shape != input_img.shape:
            anomaly_map = cv2.resize(anomaly_map, (input_img.shape[0], input_img.shape[1]))
        anomaly_map_norm = min_max_norm(anomaly_map)
        anomaly_map_norm_hm = cvt2heatmap(anomaly_map_norm*255)

        # anomaly map on image
        heatmap = cvt2heatmap(anomaly_map_norm*255)
        hm_on_img = heatmap_on_image(heatmap, input_img)

        # save images
        cv2.imwrite(os.path.join(self.sample_path, f'{x_type}_{file_name}.jpg'), input_img)
        cv2.imwrite(os.path.join(self.sample_path, f'{x_type}_{file_name}_amap.jpg'), anomaly_map_norm_hm)
        cv2.imwrite(os.path.join(self.sample_path, f'{x_type}_{file_name}_amap_on_img.jpg'), hm_on_img)
        cv2.imwrite(os.path.join(self.sample_path, f'{x_type}_{file_name}_gt.jpg'), gt_img)

    def configure_optimizers(self):
        return None

    def on_train_start(self):
        self.model.eval() # to stop running_var move (maybe not critical)
        self.embedding_dir_path, self.sample_path, self.source_code_save_path = prep_dirs(self.logger.log_dir, self.args.category)
        self.embedding_list = []
    
    def on_test_start(self):
        self.index = faiss.read_index(os.path.join(self.embedding_dir_path,'index.faiss'))
        if torch.cuda.is_available():
            res = faiss.StandardGpuResources()
            self.index = faiss.index_cpu_to_gpu(res, 0 ,self.index)
        self.init_results_list()
        self.embedding_dir_path, self.sample_path, self.source_code_save_path = prep_dirs(self.logger.log_dir, self.args.category)
        
    def training_step(self, batch, batch_idx): # save locally aware patch features
        x, _, _, file_name, _ = batch
        features = self(x)
        
        if '+' in self.args.block_index :        
            embeddings = []
            m = torch.nn.AvgPool2d(3, 1, 1)
            for feature in features:               
                embeddings.append(m(feature))
            embedding_ = np.array(embedding_concat(embeddings[0], embeddings[1]))
        else :
            embedding_ = np.array(features[0].cpu())
            
        self.embedding_list.extend(reshape_embedding(embedding_))

    def training_epoch_end(self, outputs): 
        total_embeddings = np.array(self.embedding_list)
        # Random projection
        self.randomprojector = SparseRandomProjection(n_components='auto', eps=0.9) # 'auto' => Johnson-Lindenstrauss lemma
        self.randomprojector.fit(total_embeddings)
        
        # Coreset Subsampling
        selector = kCenterGreedy(total_embeddings, 0, 0)
        selected_idx = selector.select_batch(model=self.randomprojector, already_selected=[], N=int(total_embeddings.shape[0]*self.args.coreset_sampling_ratio))
        self.embedding_coreset = total_embeddings[selected_idx]
        
        if self.args.whitening : 
            self.embedding_mean, self.embedding_std = np.mean(self.embedding_coreset, axis=0), np.std(self.embedding_coreset, axis=0)
            self.embedding_coreset = (self.embedding_coreset - self.embedding_mean.reshape(1, -1)) / (self.args.whitening_offset + self.embedding_std.reshape(1, -1))
        if self.args.visualize_tsne : 
            self.viz_feature_list += [self.embedding_coreset[idx] for idx in range(self.embedding_coreset.shape[0])]
            self.viz_class_idx_list += [0]*self.embedding_coreset.shape[0]
        
        print('initial embedding size : ', total_embeddings.shape)
        print('final embedding size : ', self.embedding_coreset.shape)
        
        #faiss
        self.index = faiss.IndexFlatL2(self.embedding_coreset.shape[1])
        self.index.add(self.embedding_coreset) 
        faiss.write_index(self.index, os.path.join(self.embedding_dir_path,'index.faiss'))

    def test_step(self, batch, batch_idx): # Nearest Neighbour Search
        x, gt, label, file_name, x_type = batch
        
        # extract embedding
        features = self(x)
        
        if '+' in self.args.block_index : 
            embeddings = []
            m = torch.nn.AvgPool2d(3, 1, 1)
            for feature in features:
                embeddings.append(m(feature))
            embedding_ = np.array(embedding_concat(embeddings[0], embeddings[1]))
        else :
            embedding_ = np.array(features[0].cpu())
                
        embedding_test = np.array(reshape_embedding(embedding_))
        
        if self.args.whitening : 
            embedding_test = (embedding_test - self.embedding_mean.reshape(1, -1)) / (self.args.whitening_offset + self.embedding_std.reshape(1, -1))
            
        if self.args.visualize_tsne :
            self.viz_feature_list += [embedding_test[idx] for idx in range(embedding_test.shape[0])]
            self.viz_class_idx_list += [label.cpu().numpy()[0]] * embedding_test.shape[0]
                
        score_patches, feature_indices = self.index.search(embedding_test, k=1)
        score_patches = np.sqrt(score_patches)
        
        anomaly_max_idx = np.argmax(score_patches[:, 0])
        max_dist_score = score_patches[anomaly_max_idx, 0] # maximum distance score
        mean_dist_score = np.mean(score_patches[:, 0])
        anomaly_max_feature = embedding_test[anomaly_max_idx]
        nearest_patch_feature = self.index.reconstruct(feature_indices[anomaly_max_idx].item()) # nearest patch-feature from anomaly_max_feature
        _, b_nearest_patch_feature_indices = self.index.search(nearest_patch_feature.reshape(1, -1) , k=self.args.n_neighbors)
        
        neighbor_index = faiss.IndexFlatL2(self.embedding_coreset.shape[1])
        
        for i in range(b_nearest_patch_feature_indices.shape[1]) :
            neighbor_index.add(self.index.reconstruct(b_nearest_patch_feature_indices[0, i].item()).reshape(1, -1))
        
        neighbor_distances, _ = neighbor_index.search(anomaly_max_feature.reshape(1, -1), k=self.args.n_neighbors)
        neighbor_distances = np.sqrt(neighbor_distances)
        w = 1 - 1 / np.sum(np.exp(neighbor_distances - max_dist_score))
        
        score = w * max_dist_score # Image-level score
        #score = mean_dist_score # simplified Image-level score
        gt_np = gt.cpu().numpy()[0,0].astype(int)
        
        if self.args.block_index == '1+2':
            anomaly_map = score_patches[:,0].reshape((56,56))
        elif self.args.block_index == '2+3':
            anomaly_map = score_patches[:,0].reshape((28,28))
        elif self.args.block_index == '3+4':
            anomaly_map = score_patches[:,0].reshape((14,14))
        elif self.args.block_index == '5' :
            anomaly_map = score_patches[:,0].reshape((1,1))
        elif self.args.block_index == '4' :
            anomaly_map = score_patches[:,0].reshape((7,7))
        
        anomaly_map_resized = cv2.resize(anomaly_map, (self.args.input_size, self.args.input_size))
        anomaly_map_resized_blur = gaussian_filter(anomaly_map_resized, sigma=4)
        
        self.gt_list_px_lvl.extend(gt_np.ravel())
        self.pred_list_px_lvl.extend(anomaly_map_resized_blur.ravel())
        self.gt_list_img_lvl.append(label.cpu().numpy()[0])
        self.pred_list_img_lvl.append(score)
        self.img_path_list.extend(file_name)
        self.img_type_list.append(x_type[0])
        
        # save images
        x = self.inv_normalize(x).clip(0,1)
        input_x = cv2.cvtColor(x.permute(0,2,3,1).cpu().numpy()[0]*255, cv2.COLOR_BGR2RGB)
        self.save_anomaly_map(anomaly_map_resized_blur, input_x, gt_np*255, file_name[0], x_type[0])

    def test_epoch_end(self, outputs):
        print("Total pixel-level auc-roc score :")
        pixel_auc = roc_auc_score(self.gt_list_px_lvl, self.pred_list_px_lvl)
        print(pixel_auc)
        print("Total image-level auc-roc score :")
        
        img_auc = roc_auc_score(self.gt_list_img_lvl, self.pred_list_img_lvl)
        print(img_auc)
        print('test_epoch_end')
        values = {'pixel_auc': pixel_auc, 'img_auc': img_auc}
        
        if self.args.visualize_tsne:
            visualize_TSNE(self.viz_feature_list, self.viz_class_idx_list, os.path.join(self.logger.log_dir, "visualize_TSNE.png"))
        
        self.log_dict(values)
        # anomaly_list = []
        # normal_list = []
        # for i in range(len(self.gt_list_img_lvl)):
        #     if self.gt_list_img_lvl[i] == 1:
        #         anomaly_list.append(self.pred_list_img_lvl[i])
        #     else:
        #         normal_list.append(self.pred_list_img_lvl[i])

        # # thresholding
        # # cal_confusion_matrix(self.gt_list_img_lvl, self.pred_list_img_lvl, img_path_list = self.img_path_list, thresh = 0.00097)
        # # print()
        # with open(self.args.project_root_path + r'/results.txt', 'a') as f:
        #     f.write(self.args.category + ' : ' + str(values) + '\n')