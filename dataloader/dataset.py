import os
import numpy as np
from numpy.lib import recfunctions as rfn
import cv2
from models.functions.voxel_generator import VoxelGenerator
import jittor as jt
from jittor.dataset import Dataset
cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)  # close mulit-processing of open-cv
import numba as nb

def getDataloader(name):
    dataset_dict = {"Prophesee": Prophesee}
    return dataset_dict.get(name)


class Prophesee:
    def __init__(self, root, object_classes, height, width, mode="training",
                 voxel_size=None, max_num_points=None, max_voxels=None, resize=None, num_bins=None):
        """
        Creates an iterator over the Prophesee object recognition dataset.

        :param root: path to dataset root
        :param object_classes: list of string containing objects or "all" for all classes
        :param height: height of dataset image
        :param width: width of dataset image
        :param mode: "training", "testing" or "validation"
        :param voxel_size: 
        :param max_num_points: 
        :param max_voxels: 
        :param num_bins: 
        """
        if mode == "training":
            mode = "train"
        elif mode == "validation":
            mode = "val"
        elif mode == "testing":
            mode = "test"

        self.root = root
        self.mode = mode
        self.width = width
        self.height = height

        self.voxel_size = voxel_size
        self.max_num_points = max_num_points
        self.max_voxels = max_voxels
        self.num_bins = num_bins

        self.voxel_generator = VoxelGenerator(voxel_size=self.voxel_size, point_cloud_range=[0, 0, 0, resize, resize, num_bins-1],
                                              max_num_points=self.max_num_points, max_voxels=self.max_voxels)
        self.resize = resize
        self.max_nr_bbox = 60

        filelist_path = os.path.join(self.root, self.mode)

        self.event_files, self.label_files, self.index_files = self.load_data_files(filelist_path, self.root, self.mode)

        assert len(self.event_files) == len(self.label_files)

        self.object_classes = object_classes
        self.nr_classes = len(self.object_classes)  # 7 classes

        self.nr_samples = len(self.event_files)
        self.total_len = self.nr_samples
        # self.nr_samples = len(self.event_files) - len(self.index_files)*batch_size
        self.collate_batch = None
    def __len__(self):
        return len(self.event_files)

    def __getitem__(self, idx):
        """
        returns events and label, loading events from split .npy files
        :param idx:
        :return: events: (x, y, t, p)
                 boxes: (N, 4), which is consist of (x_min, y_min, x_max, y_max)
                 histogram: (512, 512, 10)
        """
        boxes_list, pos_event_list, neg_event_list = [], [], []
        bbox_file = str(self.label_files[idx])  # 显式转换为str类型
        event_file = str(self.event_files[idx])  # 显式转换为str类型
        labels_np = np.load(bbox_file)
        events_np = np.load(event_file)


        for npz_num in range(len(labels_np)):
            const_size_box = np.ones([self.max_nr_bbox, 5]) * -1
            ev_npz = "e" + str(npz_num)
            lb_npz = "l" + str(npz_num)
            events_np_ = events_np[ev_npz]
            labels_np_ = labels_np[lb_npz]
            mask = (events_np_['x'] < 1280) * (events_np_['y'] < 720)  # filter events which are out of bounds
            events_np_ = events_np_[mask]
            labels = rfn.structured_to_unstructured(labels_np_)[:, [1, 2, 3, 4, 5]]  # (x, y, w, h, class_id)
            events = rfn.structured_to_unstructured(events_np_)[:, [1, 2, 0, 3]]  # (x, y, t, p)

            labels = self.cropToFrame(labels)
            labels = self.filter_boxes(labels, 60, 20)  # filter small boxes

            # downsample and resolution=1280x720 -> resolution=512x512
            events = self.downsample_event_stream(events)

            labels[:, 2] += labels[:, 0]
            labels[:, 3] += labels[:, 1]  # [x1, y1, x2, y2, class]
            labels[:, 0] /= 1280
            labels[:, 1] /= 720
            labels[:, 2] /= 1280
            labels[:, 3] /= 720

            labels[:, :4] *= 512
            labels[:, 2] -= labels[:, 0]
            labels[:, 3] -= labels[:, 1]

            labels[:, 2:-1] += labels[:, :2]  # [x_min, y_min, x_max, y_max, class_id]

            pos_events = events[events[:, -1] == 1.0]
            neg_events = events[events[:, -1] == 0.0]
            pos_events = pos_events.astype(np.float32)
            neg_events = neg_events.astype(np.float32)


            if not len(neg_events):  # empty
                neg_events = pos_events
            if not len(pos_events):  # empty
                pos_events = neg_events

            pos_voxels, pos_coordinates, pos_num_points = self.voxel_generator.generate(pos_events[:, :3],
                                                                                        self.max_voxels)
            neg_voxels, neg_coordinates, neg_num_points = self.voxel_generator.generate(neg_events[:, :3],
                                                                                        self.max_voxels)
            # 添加数据过滤
            if pos_voxels.shape[0] == 0 or neg_voxels.shape[0] == 0:
                continue

            boxes = labels.astype(np.float32)
            const_size_box[:boxes.shape[0], :] = boxes
            boxes_list.append(const_size_box.astype(np.float32))
            pos_event_list.append(
                [jt.array(pos_voxels), jt.array(pos_coordinates), jt.array(pos_num_points)])
            neg_event_list.append(
                [jt.array(neg_voxels), jt.array(neg_coordinates), jt.array(neg_num_points)])

        # # 如果没有有效数据，返回空数据
        # if not boxes_list:
        #     empty_box = np.ones([self.max_nr_bbox, 5]) * -1
        #     empty_voxel = np.zeros((1, 6, 4, 5))
        #     empty_coord = np.zeros((1, 3))
        #     empty_num = np.zeros((1,))
        #     return (np.array([empty_box]),
        #             [[jt.array(empty_voxel), jt.array(empty_coord), jt.array(empty_num)]],
        #             [[jt.array(empty_voxel), jt.array(empty_coord), jt.array(empty_num)]])
        #
        boxes = np.array(boxes_list)
        return boxes, pos_event_list, neg_event_list

    def downsample_event_stream(self, events):
        events[:, 0] = events[:, 0] / 1280 * 512  # x
        events[:, 1] = events[:, 1] / 720 * 512  # y
        delta_t = events[-1, 2] - events[0, 2]
        events[:, 2] = 4 * (events[:, 2] - events[0, 2]) / delta_t

        _, ev_idx = np.unique(events[:, :2], axis=0, return_index=True)
        downsample_events = events[ev_idx]
        ev = downsample_events[np.argsort(downsample_events[:, 2])]
        return ev

    def normalize(self, histogram):
        """standard normalize"""
        nonzero_ev = (histogram != 0)
        num_nonzeros = nonzero_ev.sum()
        if num_nonzeros > 0:
            mean = histogram.sum() / num_nonzeros
            stddev = np.sqrt((histogram ** 2).sum() / num_nonzeros - mean ** 2)
            histogram = nonzero_ev * (histogram - mean) / (stddev + 1e-8)
        return histogram

    def cropToFrame(self, np_bbox):
        """Checks if bounding boxes are inside frame. If not crop to border"""
        boxes = []
        for box in np_bbox:
            # if box[2] > 1280 or box[3] > 800:  # filter error label
            if box[2] > 1280:
                continue

            if box[0] < 0:  # x < 0 & w > 0
                box[2] += box[0]
                box[0] = 0
            if box[1] < 0:  # y < 0 & h > 0
                box[3] += box[1]
                box[1] = 0
            if box[0] + box[2] > self.width:  # x+w>1280
                box[2] = self.width - box[0]
            if box[1] + box[3] > self.height:  # y+h>720
                box[3] = self.height - box[1]

            if box[2] > 0 and box[3] > 0 and box[0] < self.width and box[1] <= self.height:
                boxes.append(box)
        boxes = np.array(boxes).reshape(-1, 5)
        return boxes

    def filter_boxes(self, boxes, min_box_diag=60, min_box_side=20):
        """Filters boxes according to the paper rule.
        To note: the default represents our threshold when evaluating GEN4 resolution (1280x720)
        To note: we assume the initial time of the video is always 0
        :param boxes: (np.ndarray)
                     structured box array with fields ['t','x','y','w','h','class_id','track_id','class_confidence']
                     (example BBOX_DTYPE is provided in src/box_loading.py)
        Returns:
            boxes: filtered boxes
        """
        width = boxes[:, 2]
        height = boxes[:, 3]
        diag_square = width ** 2 + height ** 2
        mask = (diag_square >= min_box_diag ** 2) * (width >= min_box_side) * (height >= min_box_side)
        return boxes[mask]

    @staticmethod
    # @nb.jit()
    def load_data_files(filelist_path, root, mode):
        idx = 0
        event_files = []
        label_files = []
        index_files = []
        filelist_dir = sorted(os.listdir(filelist_path))
        for filelist in filelist_dir:
            event_path = os.path.join(root, mode, filelist, "events")
            label_path = os.path.join(root, mode, filelist, "labels")
            data_dirs = sorted(os.listdir(event_path))


            for dirs in data_dirs:
                event_path_sub = os.path.join(event_path, dirs)
                label_path_sub = os.path.join(label_path, dirs)
                event_path_list = sorted(os.listdir(event_path_sub))
                label_path_list = sorted(os.listdir(label_path_sub))
                idx += len(event_path_list) - 1
                index_files.append(idx)

                for ev, lb in zip(event_path_list, label_path_list):
                    event_root = os.path.join(event_path_sub, ev)
                    label_root = os.path.join(label_path_sub, lb)
                    event_files.append(event_root)
                    label_files.append(label_root)
        return event_files, label_files, index_files

    def file_index(self):
        return self.index_files








