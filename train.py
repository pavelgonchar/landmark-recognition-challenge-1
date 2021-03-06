# (C) 2018 Andres Torrubia, licensed under GNU General Public License v3.0 
# See license.txt

import argparse
import glob
import numpy as np
import pandas as pd
import random
from os.path import join
from pathlib import Path

from sklearn.model_selection import train_test_split
from sklearn.utils import class_weight

from keras.optimizers import Adam, Adadelta, SGD
from keras.callbacks import ModelCheckpoint, ReduceLROnPlateau, Callback
from keras.models import load_model, Model
from keras.layers import concatenate, Lambda, Input, Dense, Dropout, Flatten, Conv2D, MaxPooling2D, \
        BatchNormalization, Activation, GlobalAveragePooling2D, AveragePooling2D, Reshape, SeparableConv2D
from keras.utils import to_categorical
from keras.applications import *
from keras import backend as K
from keras.engine.topology import Layer
import keras.losses
from keras.utils import CustomObjectScope
from keras.utils.data_utils import get_file
from multi_gpu_keras import multi_gpu_model
#from keras.utils import multi_gpu_model

import skimage
from iterm import show_image

from tqdm import tqdm
from PIL import Image
from io import BytesIO
import copy
import itertools
import re
import os
import sys
import jpeg4py as jpeg
from scipy import signal
import cv2
import math
import csv
from multiprocessing import Pool
from multiprocessing import cpu_count, Process, Queue, JoinableQueue, Lock

from functools import partial
from itertools import  islice
from conditional import conditional

from collections import defaultdict
import copy

import imgaug as ia
from imgaug import augmenters as iaa
import sharedmem
from hadamard import HadamardClassifier
from clr_callback import CyclicLR
from kerassurgeon.operations import delete_layer, insert_layer, delete_channels

from extra import *
import inspect

SEED = 42

np.random.seed(SEED)
random.seed(SEED)
# TODO tf seed

parser = argparse.ArgumentParser()
# general
parser.add_argument('--max-epoch', type=int, default=1000, help='Epoch to run')
parser.add_argument('-g', '--gpus', type=int, default=None, help='Number of GPUs to use')
parser.add_argument('-v', '--verbose', action='store_true', help='Pring debug/verbose info')
parser.add_argument('-b', '--batch-size', type=int, default=48, help='Batch Size during training, e.g. -b 64')
parser.add_argument('-l', '--learning-rate', type=float, default=None, help='Initial learning rate')
parser.add_argument('-clr', '--cyclic_learning_rate',action='store_true', help='Use cyclic learning rate https://arxiv.org/abs/1506.01186')
parser.add_argument('-o', '--optimizer', type=str, default='adam', help='Optimizer to use in training -o adam|sgd|adadelta')
parser.add_argument('--amsgrad', action='store_true', help='Apply the AMSGrad variant of adam|adadelta from the paper "On the Convergence of Adam and Beyond".')

# architecture/model
parser.add_argument('-m', '--model', help='load hdf5 model including weights (and continue training)')
parser.add_argument('-w', '--weights', help='load hdf5 weights only (and continue training)')
parser.add_argument('-do', '--dropout', type=float, default=0., help='Dropout rate for first FC layer')
parser.add_argument('-dol', '--dropout-last', type=float, default=0., help='Dropout rate for last FC layer')
parser.add_argument('-doc', '--dropout-classifier', type=float, default=0., help='Dropout rate for classifier')
parser.add_argument('-nfc', '--no-fcs', action='store_true', help='Dont add any FC at the end, just a softmax')
parser.add_argument('-fc', '--fully-connected-layers', nargs='+', type=int, default=[512,256], help='Specify FC layers after classifier, e.g. -fc 1024 512 256')
parser.add_argument('-f', '--freeze', type=int, default=0, help='Freeze first n CNN layers, e.g. --freeze 10')
parser.add_argument('-fu', '--freeze-until', type=str, default=None, help='Freeze until named CNN layer, e.g. --freeze-until 10')
parser.add_argument('-fca', '--fully-connected-activation', type=str, default='relu', help='Activation function to use in FC layers, e.g. -fca relu|selu|prelu|leakyrelu|elu|...')
parser.add_argument('-bn', '--batch-normalization', action='store_true', help='Use batch normalization in FC layers')
parser.add_argument('-cm', '--classifier', type=str, default='ResNet50', help='Base classifier model to use')
parser.add_argument('-pcs', '--print-classifier-summary', action='store_true', help='Print classifier model summary')
parser.add_argument('-uiw', '--use-imagenet-weights', action='store_true', help='Use imagenet weights (transfer learning)')
parser.add_argument('-p', '--pooling', type=str, default='avg', help='Type of pooling to use: avg|max|none')
parser.add_argument('-rp', '--reduce-pooling', type=int, default=None, help='If using pooling none add conv layers to reduce features, e.g. -rp 128')
parser.add_argument('-lo', '--loss', type=str, default='categorical_crossentropy', help='Loss function')
parser.add_argument('-hp', '--hadamard', action='store_true', help='Use Hadamard projection instead of FC layers, see https://arxiv.org/pdf/1801.04540.pdf')
parser.add_argument('-l2', '--l2-normalize', action='store_true', help='Perform L2 normalization in Hadamard classifier')
parser.add_argument('-pp', '--post-pooling', type=str, default=None, help='Add pooling layers after classifier, e.g. -pp avg|max')
parser.add_argument('-pps', '--post-pool-size', type=int, default=2, help='Pooling factor for pooling layers after classifier, e.g. -pps 3')
parser.add_argument('-dl', '--delete-layers', nargs='+', type=str, default=[], help='Specify layers to delete in classifier, e.g. -dl avg_pool')
parser.add_argument('-nd', '--no-dense', action='store_true', help='Dont add any Dense layer at the end')
parser.add_argument('-bf', '--bottleneck-features', type=int, default=16384, help='If classifier supports it, override number of bottleneck feautures (typically 2048)')
parser.add_argument('-pcf', '--project-classifier-features', type=int, default=0, help='For -id project classifier features into subspace of given size, e.g. -pcf 512')
parser.add_argument('-tl', '--triplet-loss', action='store_true', help='Use triplet loss to train feature extractor model')

# training regime
parser.add_argument('-cs', '--crop-size', type=int, default=256, help='Crop size')
parser.add_argument('-vpc', '--val-percent', type=float, default=0.15, help='Val percent')
parser.add_argument('-ps', '--pavel-split', action='store_true', help='Use Pavel validation split trick')
parser.add_argument('-fcm', '--freeze-classifier', action='store_true', help='Freeze classifier weights (useful to fine-tune FC layers)')
parser.add_argument('-fac', '--freeze-all-classifiers', action='store_true', help='Freeze all classifier (feature extractor) weights when using -id')
parser.add_argument('-t25', '--top25', action='store_true', help='top 25%')

# augmentations
parser.add_argument('-aa', '--augment-always', action='store_true', help='If set will try to augment (based on prob) always (does not wait until 1st seen sample)')
parser.add_argument('-naa', '--no-auto-augment', action='store_true', help='Dont force auto-augment always (e.g. with -w or -l)')
parser.add_argument('-aps', '--augmentation-probability-soft', type=float, default=1., help='Probability of soft augmentations after 1st seen sample (or always w/ -aa)')
parser.add_argument('-aph', '--augmentation-probability-hard', type=float, default=0.5, help='Probability of hard augmentations after 1st seen sample (or always w/ -aa)')

# training regime (class aware sampling options)
parser.add_argument('-cas', '--class-aware-sampling', action='store_true', help='Use class aware sampling to balance dataset (instead of class weights)')
parser.add_argument('-casac', '--class-aware-sampling-accuracy-target', type=float, default=0.9, help='Threshold to move to next landmark group (when using -cas)')
parser.add_argument('-casab', '--class-aware-sampling-accuracy-batches', type=int, default=20, help='Number of batches to check for threshold to move to next landmark group (when using -cas)')
parser.add_argument('-casp',  '--class-aware-sampling-patience', type=int, default=8000, help='Patience in number of items to move to nexty landmark_to_cat (when using -cas)')
parser.add_argument('-casr', '--class-aware-sampling-resume', type=int, default=0, help='Resume from group, e.g. -casr 8 (group starts at 0)')

# dataset (training)
parser.add_argument('-id', '--include-distractors', action='store_true', help='Include distractors')
parser.add_argument('-ri', '--remove-indoor', action='store_true', help='Remove indoor images from the traning set')
parser.add_argument('-p1365', '--vgg-places1365', action='store_true', help='Use VGG16PlacesHybrid1365 features for distractor training')
parser.add_argument('-p365',  '--vgg-places365', action='store_true', help='Use VGG16Places365 features for distractor training')
parser.add_argument('-tk',  '--top-k', type=int, default=0, help='Only keep top-k logits from feature extractor -tk 512')

# test
parser.add_argument('-t', '--test', action='store_true', help='Test model and generate CSV/npy submission file')
parser.add_argument('-tt', '--test-train', action='store_true', help='Test model on the training set')
parser.add_argument('-th', '--threshold', default=0., type=float, help='Ignore predictions less than threshold, e.g. -th 0.6')
parser.add_argument('-ssd',  '--scale-score-distractors', action='store_true', help='Scale softmax score by distractor soft-prob')

# NN related
parser.add_argument('-knn', '--knn', action='store_true', help='Test model using distance metric')
parser.add_argument('-knnls', '--knn-landmark-samples', default=8, type=int, help='Max number of samples to compute features for each landmark')
parser.add_argument('--test-csv', default='test.csv', help='Override test.csv')
parser.add_argument('--train-csv', default='train.csv', help='Override train.csv')
parser.add_argument('--test-dir', default='test-dl', help='Override test images directory')
parser.add_argument('--train-dir', default='train-dl', help='Override train images directory')
parser.add_argument('--features-dir', default='features', help='Where to save computed features')

args = parser.parse_args()

training = not (args.test or args.test_train)

if not args.verbose:
    import warnings
    warnings.filterwarnings("ignore")

if args.hadamard:
    args.no_fcs = True
    print("Info: auto-setting --no-fcs because --hadamard")

if args.include_distractors:
    args.freeze_classifier = True
    print("Info: auto-setting --freeze-classifier because --include-distractors")

if (args.model or args.weights) and (not args.triplet_loss) and training and (not args.no_auto_augment):
    args.augment_always = True
    print("Info: auto-setting --augment-always because -m or -w")

from tensorflow.python.client import device_lib
def get_available_gpus():
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

if args.gpus is None:
    args.gpus = len(get_available_gpus())   

args.batch_size *= max(args.gpus, 1)

TRAIN_DIR    = args.train_dir
TRAIN_JPGS   = set(Path(TRAIN_DIR).glob('*.jpg'))
TRAIN_IDS    = { os.path.splitext(os.path.basename(item))[0] for item in TRAIN_JPGS }

if args.test:
    TEST_DIR     = args.test_dir
    TEST_JPGS    = list(Path(TEST_DIR).glob('*.jpg'))
    TEST_IDS     = { os.path.splitext(os.path.basename(item))[0] for item in TEST_JPGS  }

MODEL_FOLDER        = 'models'
CSV_FOLDER          = 'csv'
TRAIN_CSV           = args.train_csv
TEST_CSV            = args.test_csv

if args.include_distractors:
    NON_LANDMARK_DISTRACTOR_JPGS  = list(Path('distractors').glob('*.jpg'))
    NON_LANDMARK_DISTRACTOR_JPGS += list(Path('../yelp-restaurant-photo-classification/train_photos').glob('[0-9a-z]*.jpg'))
    NON_LANDMARK_DISTRACTOR_JPGS += list(Path('open-images-dataset/train').glob('*.jpg')) 

    LANDMARK_DISTRACTOR_JPGS      = list(Path('../landmark-retrieval-challenge/train').glob('*.jpg'))[:len(NON_LANDMARK_DISTRACTOR_JPGS)]
    DISTRACTOR_JPGS = NON_LANDMARK_DISTRACTOR_JPGS + LANDMARK_DISTRACTOR_JPGS
    random.shuffle(DISTRACTOR_JPGS)
    DISTRACTOR_IDS    = { os.path.splitext(os.path.basename(item))[0] for item in DISTRACTOR_JPGS }

CROP_SIZE = args.crop_size

id_to_landmark  = { }
id_to_cat       = { }
id_times_seen   = { }

landmark_to_ids = defaultdict(list)
cat_to_ids      = defaultdict(list)
cat_to_items    = defaultdict(list)
landmark_to_cat = { }
cat_to_landmark = { }

if args.top25:
    top25 = set(open('top3750.txt').read().splitlines())

# since we may get holes in landmark (ids) from the CSV file
# we'll use cat (category) starting from 0 and keep a few dicts to map around
#cat = -1
max_landmark = -1
pavel_ids = set()
top25_ids = set()
landmarks_remap = dict()

with open(TRAIN_CSV, 'r') as csvfile:
    reader = csv.reader(csvfile, delimiter=',', quotechar='|')
    next(reader)
    for row in reader:
        idx, landmark, url = row[0][1:-1], int(row[2]), row[1]
        if args.top25 and str(landmark) not in top25:
            continue
        if idx in TRAIN_IDS:
            if landmark in landmark_to_cat:
                landmark_cat = landmark_to_cat[landmark]
            else:
                max_landmark = max(max_landmark, landmark)
                landmark_cat = landmark #cat
                landmark_to_cat[landmark] = landmark_cat
                cat_to_landmark[landmark_cat] = landmark
                #cat += 1 
            id_to_landmark[idx] = landmark
            id_to_cat[idx]      = landmark_cat
            id_times_seen[idx]  = 0
            landmark_to_ids[landmark].append(idx)
            cat_to_ids[landmark_cat].append(idx)
            if "lh3.goog" in url:
                pavel_ids.add(idx)
            top25_ids.add(idx)
 

N_CLASSES = len(landmark_to_cat.keys())

if args.include_distractors:
    landmark = -1
    landmark_cat = landmark#cat
    landmark_to_cat[landmark] = landmark_cat
    cat_to_landmark[landmark_cat] = landmark

    for idx in DISTRACTOR_IDS:
        id_to_landmark[idx] = landmark
        id_to_cat[idx]      = landmark_cat
        id_times_seen[idx]  = 0
        landmark_to_ids[landmark].append(idx)
        cat_to_ids[landmark_cat].append(idx)

#print(len(id_to_landmark.keys()), N_CLASSES, max_landmark)
#assert N_CLASSES == (max_landmark +1)
keys = list(landmark_to_cat.keys())
for i in range(N_CLASSES):
    landmarks_remap[keys[i]] = i

def get_class(item):
    #return id_to_cat[os.path.splitext(os.path.basename(item))[0]]
    if args.top25:
        try:
            label = landmarks_remap[id_to_cat[os.path.splitext(os.path.basename(item))[0]]]
        except:
            label = -1
        return label
    else:
        return id_to_cat[os.path.splitext(os.path.basename(item))[0]]

def get_id(item):
    return os.path.splitext(os.path.basename(item))[0]

# since we are doing stratified train/val split we need to dupe images
# from landmarks with just 1 item
ids_to_dup = [ids[0] for cat,ids in cat_to_ids.items() if len(ids) == 1]

print(len(ids_to_dup))

TRAIN_JPGS = list(TRAIN_JPGS) + ids_to_dup 

if args.include_distractors:

    print("Total items {} of which {:.2f}% are distractors".format(
        len(TRAIN_JPGS) + len(DISTRACTOR_JPGS), 
        100. * len(DISTRACTOR_JPGS) / (len(TRAIN_JPGS) + len(DISTRACTOR_JPGS))))
else:
    print("Total items in set {}".format(
        len(TRAIN_JPGS), ))

TRAIN_CATS = [ get_class(idx) for idx in TRAIN_JPGS ]
TRAIN_JPGS = [ idx for idx in TRAIN_JPGS if get_class(idx) != -1 ]
TRAIN_CATS = [ e for e in TRAIN_CATS if e != -1 ]

def preprocess_image(img):
    
    # find `preprocess_input` function specific to the classifier
    classifier_to_module = { 
        'NASNetLarge'       : 'nasnet',
        'NASNetMobile'      : 'nasnet',
        'DenseNet121'       : 'densenet',
        'DenseNet161'       : 'densenet',
        'DenseNet201'       : 'densenet',
        'InceptionResNetV2' : 'inception_resnet_v2',
        'InceptionV3'       : 'inception_v3',
        'MobileNet'         : 'mobilenet',
        'ResNet50'          : 'resnet50',
        'VGG16'             : 'vgg16',
        'VGG19'             : 'vgg19',
        'Xception'          : 'xception',

        'VGG16Places365'        : 'vgg16_places365',
        'VGG16PlacesHybrid1365' : 'vgg16_places_hybrid1365',

        'SEDenseNetImageNet121' : 'se_densenet',
        'SEDenseNetImageNet161' : 'se_densenet',
        'SEDenseNetImageNet169' : 'se_densenet',
        'SEDenseNetImageNet264' : 'se_densenet',
        'SEInceptionResNetV2'   : 'se_inception_resnet_v2',
        'SEMobileNet'           : 'se_mobilenets',
        'SEResNet50'            : 'se_resnet',
        'SEResNet101'           : 'se_resnet',
        'SEResNet154'           : 'se_resnet',
        'SEInceptionV3'         : 'se_inception_v3',
        'SEResNext'             : 'se_resnet',
        'SEResNextImageNet'     : 'se_resnet',

        'ResNet152'             : 'resnet152',
        'AResNet50'             : 'aresnet50',
        'AXception'             : 'axception',
        'AInceptionV3'          : 'ainceptionv3',
    }

    if args.classifier in classifier_to_module:
        classifier_module_name = classifier_to_module[args.classifier]
    else:
        classifier_module_name = 'xception'

    preprocess_input_function = getattr(globals()[classifier_module_name], 'preprocess_input')
    return preprocess_input_function(img.astype(np.float32))

def augment_soft(img):
    # Sometimes(0.5, ...) applies the given augmenter in 50% of all cases,
    # e.g. Sometimes(0.5, GaussianBlur(0.3)) would blur roughly every second image.
    sometimes = lambda aug: iaa.Sometimes(0.5, aug)

    # Define our sequence of augmentation steps that will be applied to every image
    # All augmenters with per_channel=0.5 will sample one value _per image_
    # in 50% of all cases. In all other cases they will sample new values
    # _per channel_.
    seq = iaa.Sequential(
        [
            # apply the following augmenters to most images
            iaa.Fliplr(0.5), # horizontally flip 50% of all images
            # crop images by -5% to 10% of their height/width
            iaa.Crop(
                percent=(0, 0.2),
            ),
            iaa.Scale({"height": CROP_SIZE, "width": CROP_SIZE }),
        ],
        random_order=False
    )

    if img.ndim == 3:
        img = seq.augment_images(np.expand_dims(img, axis=0)).squeeze(axis=0)
    else:
        img = seq.augment_images(img)

    return img

def augment_hard(img):
    # Sometimes(0.5, ...) applies the given augmenter in 50% of all cases,
    # e.g. Sometimes(0.5, GaussianBlur(0.3)) would blur roughly every second image.
    sometimes = lambda aug: iaa.Sometimes(0.5, aug)

    # Define our sequence of augmentation steps that will be applied to every image
    # All augmenters with per_channel=0.5 will sample one value _per image_
    # in 50% of all cases. In all other cases they will sample new values
    # _per channel_.
    seq = iaa.Sequential(
        [
            # apply the following augmenters to most images
            iaa.Fliplr(0.5), # horizontally flip 50% of all images
            # crop images by -5% to 10% of their height/width
            sometimes(iaa.Crop(
                percent=(0, 0.2),
            )),
            sometimes(iaa.Affine(
                scale={"x": (1, 1.2), "y": (1, 1.2)}, # scale images to 80-120% of their size, individually per axis
                translate_percent={"x": (-0.1, 0.1), "y": (-0.1, 0.1)}, # translate by -20 to +20 percent (per axis)
                rotate=(-5, 5), # rotate by -45 to +45 degrees
                shear=(-5, 5), # shear by -16 to +16 degrees
                order=[0, 1], # use nearest neighbour or bilinear interpolation (fast)
                cval=(0, 255), # if mode is constant, use a cval between 0 and 255
                mode="reflect" # use any of scikit-image's warping modes (see 2nd image from the top for examples)
            )),
            # execute 0 to 5 of the following (less important) augmenters per image
            # don't execute all of them, as that would often be way too strong
            iaa.SomeOf((0, 1),
                [
                    iaa.OneOf([
                        iaa.GaussianBlur((0, 2.0)), # blur images with a sigma between 0 and 3.0
                        iaa.AverageBlur(k=(2, 5)), # blur image using local means with kernel sizes between 2 and 7
                    ]),
                    iaa.Sharpen(alpha=(0, 1.0), lightness=(0.75, 1.5)), # sharpen images
                    # search either for all edges or for directed edges,
                    # blend the result with the original image using a blobby mask
                    iaa.Add((-10, 10), per_channel=0.5), # change brightness of images (by -10 to 10 of original value)
                    iaa.AddToHueAndSaturation((-20, 20)), # change hue and saturation
                    # either change the brightness of the whole image (sometimes
                    # per channel) or change the brightness of subareas
                    iaa.OneOf([
                        iaa.Multiply((0.5, 1.5), per_channel=0.5),
                        iaa.FrequencyNoiseAlpha(
                            exponent=(-4, 0),
                            first=iaa.Multiply((0.5, 1.5), per_channel=True),
                            second=iaa.ContrastNormalization((0.5, 2.0))
                        )
                    ]),
                    iaa.ContrastNormalization((0.5, 2.0), per_channel=0.5), # improve or worsen the contrast
                    iaa.Grayscale(alpha=(0.0, 1.0)),
                    sometimes(iaa.PiecewiseAffine(scale=(0.01, 0.03))), # sometimes move parts of the image around
                    sometimes(iaa.PerspectiveTransform(scale=(0.01, 0.1)))
                ],
                random_order=True
            ),
            iaa.Scale({"height": CROP_SIZE, "width": CROP_SIZE }),
        ],
        random_order=False
    )

    if img.ndim == 3:
        img = seq.augment_images(np.expand_dims(img, axis=0)).squeeze(axis=0)
    else:
        img = seq.augment_images(img)

    return img


# reads the image referenced by item from disk and
# returns img, one_hot_class_idx, item
# img: processed image (normalized as excepted by NN) and augmented if both aug and training are True
# one_hot_class_idx: one-hot vector of cat id (if predict is false)
# item: same as passed 
# 
# img and one_hot_class_idx will be None if error reading item
#
def process_item(item, aug = False, training = False, predict=False):

    load_img_fast_jpg  = lambda img_path: jpeg.JPEG(img_path).decode()
    load_img           = lambda img_path: np.array(Image.open(img_path))

    def try_load_PIL(item):
        try:
            img = load_img(item)
            return img
        except Exception:
            if args.verbose:
                print('Decoding error:', item)
            return None

    validation = not training 

    loaded_pil = loaded_fast_jpg = False
    try:
        img = load_img_fast_jpg(item)
        loaded_fast_jpg = True
    except Exception:
        img = try_load_PIL(item)
        if img is None: return None, None, item
        loaded_pil = True

    shape = list(img.shape[:2])

    # some images may not be downloaded correctly and are B/W, discard those
    if img.ndim != 3:
        if args.verbose:
            print('Ndims !=3 error:', item)
        if not loaded_pil:
            img = try_load_PIL(item)
            if img is None: return None, None, item
            loaded_pil = True
        if img.ndim == 2:
            img = np.stack((img,)*3, -1)
        if img.ndim != 3:
            return None, None, item

    if img.shape[2] != 3:
        if args.verbose:
            print('More than 3 channels error:', item)
        if not loaded_pil:
            img = try_load_PIL(item)
            if img is None: return None, None, item
            loaded_pil = True   
        return None, None, item

    if training and aug:
        if np.random.random() < args.augmentation_probability_hard:
            img = augment_hard(img)
        elif np.random.random() < args.augmentation_probability_soft:
            img = augment_soft(img)
        if np.random.random() < 0.0:
            show_image(img)
    else:
        img = cv2.resize(img, (CROP_SIZE, CROP_SIZE))

    img = preprocess_image(img)

    if args.verbose:
        print("ap: ", img.shape, item)

    if not predict:
        _class = get_class(item)
        one_hot_class_idx = to_categorical(_class, N_CLASSES) if _class != -1 else np.ones(N_CLASSES, dtype=np.float32)
    else:
        one_hot_class_idx = np.zeros(N_CLASSES, dtype=np.float32)

    return img, one_hot_class_idx, item

# multiprocess worker to read items and put them in shared memory for consumer
def process_item_worker(worker_id, lock, shared_mem_X, shared_mem_y, jobs, results):
    # make sure augmentations are different for each worker
    np.random.seed()
    random.seed()

    while True:
        item, aug, training, predict = jobs.get()
        img, one_hot_class_idx, item = process_item(item, aug, training, predict)
        is_good_item = False
        if one_hot_class_idx is not None:
            lock.acquire()
            shared_mem_X[worker_id,...] = img
            shared_mem_y[worker_id,...] = one_hot_class_idx
            is_good_item = True
        results.put((worker_id, is_good_item, item))

# multiprocess worker to read items and put them in shared memory for consumer
def process_item_worker_triplet(worker_id, lock, shared_mem_X, shared_mem_y, jobs, results):
    # make sure augmentations are different for each worker
    np.random.seed()
    random.seed()

    while True:
        items, augs, training, predict = jobs.get()
        img_p1, one_hot_class_idx_p1, item_p1 = process_item(items[0], augs[0], training, predict)
        img_p2, one_hot_class_idx_p2, item_p2 = process_item(items[1], augs[1], training, predict)
        img_n1, one_hot_class_idx_n1, item_n1 = process_item(items[2], augs[2], training, predict)
        is_good_item = False
        if (one_hot_class_idx_p1 is not None) and (one_hot_class_idx_p2 is not None) and (one_hot_class_idx_n1 is not None):
            lock.acquire()
            shared_mem_X[worker_id,...,0] = img_p1
            shared_mem_X[worker_id,...,1] = img_p2
            shared_mem_X[worker_id,...,2] = img_n1
            is_good_item = True
        results.put((worker_id, is_good_item, (item_p1, item_p2, item_n1)))


# Callback to monitor accuracy on a per-batch basis
class AccuracyReset(Callback):

    N_BATCHES = args.class_aware_sampling_accuracy_batches

    def __init__(self, filepath):
        super(AccuracyReset, self).__init__()
        self.filepath = filepath

    def on_train_begin(self, logs={}):

        self.aucs = []
        self.losses = []
        self.reset_accuracy()
 
    def on_train_end(self, logs={}):
        return
 
    def on_epoch_begin(self, epoch, logs={}):
        self.epoch = epoch
        return
 
    def on_epoch_end(self, epoch, logs={}):
        self.epoch = epoch
        return
 
    def on_batch_begin(self, batch, logs={}):
        #print(logs)
        return
 
    def on_batch_end(self, batch, logs={}):
        self.last_accuracies[self.last_accuracies_i % AccuracyReset.N_BATCHES ] = logs['categorical_accuracy']
        self.last_accuracies_i += 1
        #print( self.last_accuracies)
        if np.all(self.last_accuracies >= args.class_aware_sampling_accuracy_target):
            self.accuracy_reached = True
        return

    def reset_accuracy(self, group=-1, save = False):
        self.accuracy_reached = False
        self.last_accuracies = np.zeros(AccuracyReset.N_BATCHES)
        self.last_accuracies_i = 0
        if group != -1 and save:
            self.model.save(
                self.filepath.format(group= group, epoch= self.epoch + 1), 
                overwrite=True)
        return

# Callback to monitor accuracy on a per-batch basis
class MonitorDistance(Callback):

    def __init__(self):
        super(MonitorDistance, self).__init__()

    def on_train_begin(self, logs={}):
        self.input_image = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image' )
        for layer in self.model.layers:
            if isinstance(layer, Model):
                self.feature_model = layer
                break
        self.feature_model.summary()
        return
 
    def on_train_end(self, logs={}):
        return
 
    def on_epoch_begin(self, epoch, logs={}):
        return
 
    def on_epoch_end(self, epoch, logs={}):

        cats_to_monitor = range(10)
        images = np.empty((len(cats_to_monitor) * 2, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
        # 0 cat0_img0
        # 1 cat0_img1
        # 2 cat1_img0
        # 3 cat1_img1
        for i, cat in enumerate(cats_to_monitor):
            images[i * 2,    ...], _, _ = process_item(Path(TRAIN_DIR) / (cat_to_ids[cat][0] + ".jpg"))
            images[i * 2 + 1,...], _, _ = process_item(Path(TRAIN_DIR) / (cat_to_ids[cat][1] + ".jpg"))
        features = self.feature_model.predict(images)
        print(features)

        for i, cat in enumerate(cats_to_monitor):
            distance = np.linalg.norm(features[i * 2] - features[i * 2 + 1])
            print("Cat {}-{} distance: {:.4f}".format(cat, cat, distance))

        print()

        for i0, cat0 in enumerate(cats_to_monitor[:-1]):
            for i1, cat1 in enumerate(cats_to_monitor[i0+1:]):
                distance = np.linalg.norm(features[i0 * 2] - features[(i0 + i1 + 1) * 2])
                print("Cat {}-{} distance: {:.4f}".format(cat0, cat1, distance))

        return
 
    def on_batch_begin(self, batch, logs={}):
        #print(logs)
        return
 
    def on_batch_end(self, batch, logs={}):
        return

# main generator. Although predict=True mode works it is not used here.
def gen(items, batch_size, training=True, predict=False, accuracy_callback=None):

    validation = not training 
    items_set = set(items)

    if args.triplet_loss:
        Xp1 = np.empty((batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
        Xp2 = np.empty((batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
        Xn1 = np.empty((batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
    else:
        # X image crops
        X = np.empty((batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)

    if predict:
        training = False

    # class index
    y = np.zeros((batch_size, N_CLASSES),               dtype=np.float32)
    if args.include_distractors:
        d = np.empty((batch_size),                      dtype=np.float32)
    
    n_group_classes = int(math.ceil(N_CLASSES / batch_size))
    if training and (args.class_aware_sampling or args.triplet_loss):
        items_per_class = defaultdict(list)
        n_items_per_class = np.zeros(N_CLASSES, dtype=np.int64)
        for item in items:
            class_idx = get_class(item)
            items_per_class[class_idx].append(item)
            n_items_per_class[class_idx] += 1
        items_per_class_running=copy.deepcopy(items_per_class)
        classes_groups = np.array_split(np.argsort(n_items_per_class)[::-1], n_group_classes)
        classes_current_group = -1
        classes_current_group_items_to_see = 0
        classes_running_copy  = [ ]
        classes_seen = set()
        previous_classes_seen = set()
        if args.class_aware_sampling_resume != 0:
            # if resuming to group args.class_aware_sampling_resume:
            for class_group in classes_groups[:args.class_aware_sampling_resume]:
                # add classes from previous groups to previously seen classes
                previous_classes_seen = previous_classes_seen.union(class_group)
                # and for each class in group
                for class_idx in class_group:
                    # mark items up to worst case scenario (patience reached) as seen once so augmentation kicks in
                    for item in items_per_class[class_idx][:args.class_aware_sampling_patience]:
                        id_times_seen[get_id(item)] += 1
            classes_current_group = (args.class_aware_sampling_resume - 1) % n_group_classes
            print("Resuming from group {}. Landmarks marked as seen: {}".format(
                classes_current_group,
                " ".join([str(cat_to_landmark[cat]) for cat in previous_classes_seen])))
        # dont save model the first time
        save_model = False
        if args.triplet_loss:
            classes = list(range(N_CLASSES))


    n_workers    = (cpu_count() - 1) if not predict else 1 # for prediction we need to guarantee order
    if args.triplet_loss:
        shared_mem_X = sharedmem.empty((n_workers, CROP_SIZE, CROP_SIZE, 3, 3), dtype=np.float32)
        shared_mem_y = None
    else:
        shared_mem_X = sharedmem.empty((n_workers, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
        shared_mem_y = sharedmem.empty((n_workers, N_CLASSES),               dtype=np.float32)
    locks        = [Lock()] * n_workers
    jobs         = Queue(args.batch_size * 4 if not predict else 1)
    results      = JoinableQueue(args.batch_size * 2 if not predict else 1)

    [Process(
        target=process_item_worker if not args.triplet_loss else process_item_worker_triplet, 
        args=(worker_id, lock, shared_mem_X, shared_mem_y, jobs, results)).start() for worker_id, lock in enumerate(locks)]

    bad_items = set()
    i = 0

    while True:

        if training and not args.class_aware_sampling:
            random.shuffle(items)

        batch_idx = 0

        items_done  = 0
        while items_done < len(items):  
            # fill the queue to make sure CPU is always busy
            while not jobs.full():
                if training and args.class_aware_sampling:
                    if np.random.rand() >= 0.2 or not previous_classes_seen:
                        # if already reached patience or accuracy, build classes list with this group's classes
                        if classes_current_group_items_to_see == 0 or accuracy_callback.accuracy_reached:
                            print(accuracy_callback.last_accuracies)
                            accuracy_callback.reset_accuracy(classes_current_group, save = save_model)
                            # save model from now on
                            save_model = True
                            classes_current_group_items_to_see = int(args.class_aware_sampling_patience * N_CLASSES / n_group_classes)
                            classes_current_group = (classes_current_group + 1) % n_group_classes
                            classes = list(classes_groups[classes_current_group])
                            classes_running_copy = []
                            previous_classes_seen = previous_classes_seen.union(classes_seen)
                            classes_seen = set()
                            print("Class group #{} {}/{} ({:.2f}% of items)".format(
                                classes_current_group, 
                                classes_current_group+1, 
                                n_group_classes,
                                100. * sum([n_items_per_class[class_id] for class_id in previous_classes_seen.union(set(classes))]) / len(items),
                                ))
                        # if we've run out of items in this class, replenish it
                        if len(classes_running_copy) == 0:
                            random.shuffle(classes)
                            classes_running_copy = list(classes)
                        random_class = classes_running_copy.pop()
                        classes_current_group_items_to_see -= 1
                    else:
                        # pick a random class of the ones seen previously to make sure the net
                        # doesn't forget what it's learned
                        random_class = random.sample(previous_classes_seen, 1)[0]

                    # pick an item from class making sure it belongs to the items
                    # this is not strictly needed, though
                    training_item_chosen = False
                    while not training_item_chosen:
                        if len(items_per_class_running[random_class]) == 0:
                            random.shuffle(items_per_class_running[random_class])
                            items_per_class_running[random_class]=copy.deepcopy(items_per_class[random_class])
                        item = items_per_class_running[random_class].pop()
                        training_item_chosen = item in items_set
                    classes_seen.add(random_class)
                elif args.triplet_loss:
                    if len(classes_running_copy) == 0:
                        random.shuffle(classes)
                        classes_running_copy = list(classes)
                    random_classP = classes_running_copy.pop()
                    random_classN = random_classP
                    while random_classN == random_classP:
                        random_classN = random.choice(classes)

                    def pick_item_from_class(items_per_class_running, random_class):
                        if len(items_per_class_running[random_class]) == 0:
                            random.shuffle(items_per_class_running[random_class])
                            items_per_class_running[random_class]=copy.deepcopy(items_per_class[random_class])
                        return items_per_class_running[random_class].pop()

                    item_p1 = pick_item_from_class(items_per_class_running, random_classP)
                    item_p2 = pick_item_from_class(items_per_class_running, random_classP)
                    item_n1 = pick_item_from_class(items_per_class_running, random_classN)

                else:
                    # if not using class-aware sampling, just pick one item
                    if args.include_distractors:
                        pick_distractor =  np.random.random() <= 0.5
                        while True:
                            item = items[i % len(items)]
                            i += 1
                            if (get_class(item) == -1 and pick_distractor) or (get_class(item) != -1 and not pick_distractor):
                                break
                    else:
                        item = items[i % len(items)]
                        i += 1
                if not predict:
                    if args.triplet_loss:
                        augs = []
                        augs.append(False if ( (id_times_seen[get_id(item_p1)]==0) and not args.augment_always) else True)
                        augs.append(False if ( (id_times_seen[get_id(item_p2)]==0) and not args.augment_always) else True)
                        augs.append(False if ( (id_times_seen[get_id(item_n1)]==0) and not args.augment_always) else True)
                        id_times_seen[get_id(item_p1)] += 1
                        id_times_seen[get_id(item_p2)] += 1
                        id_times_seen[get_id(item_n1)] += 1
                    else:
                        # do not augment the first time the net has seen an item
                        aug = False if ( (id_times_seen[get_id(item)]==0) and not args.augment_always) else True
                        id_times_seen[get_id(item)] += 1
                else:
                    # do not augment if predicting
                    if args.triplet_loss:
                        augs = [False, False, False]
                    else:
                        aug = False
                if args.triplet_loss:
                    jobs.put(([item_p1, item_p2, item_n1], augs, training, predict))
                else:
                    jobs.put((item, aug, training, predict))
                items_done += 1

            # loop over results and yield until no more resuls left
            get_more_results = True
            while get_more_results:
                worker_id, is_good_item, _item = results.get() # blocks/waits if None
                results.task_done()

                if is_good_item:
                    if args.triplet_loss:
                        Xp1[batch_idx], Xp2[batch_idx], Xn1[batch_idx] = \
                            shared_mem_X[worker_id, ...,0], shared_mem_X[worker_id, ...,1], shared_mem_X[worker_id, ...,2]
                    else:
                        X[batch_idx], y[batch_idx] = shared_mem_X[worker_id], shared_mem_y[worker_id]
                        if args.include_distractors:
                            d[batch_idx] = 1 if np.all(shared_mem_y[worker_id] == 1.) else 0
                    locks[worker_id].release()
                    batch_idx += 1
                else:
                    if predict:
                        X[batch_idx] = np.zeros((CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)
                        batch_idx += 1
                        print("Warning {}".format(_item))
                    bad_items.add(_item)

                if batch_idx == batch_size:
                    if not predict:
                        if args.triplet_loss:
                            yield([Xp1, Xp2, Xn1], y)
                        else:
                            _Y = y if not args.include_distractors else [y,d]
                            yield(X, _Y)
                    else:
                        yield(X)
                    batch_idx = 0

                get_more_results = not results.empty()

        if len(bad_items) > 0:
            print("\nRejected {} items: {}".format('trainining' if training else 'validation', len(bad_items)))

def zero_loss(y_true, y_pred):
    return  K.zeros(shape=(1,))

def identity_loss(y_true, y_pred):
    return K.mean(y_pred)

# MAIN
if args.model:
    print("Loading model " + args.model)

    with CustomObjectScope({
        'HadamardClassifier': HadamardClassifier, 
        'zero_loss': zero_loss,
        'identity_loss' : identity_loss}):
        model = load_model(args.model, compile=False if not training or (args.learning_rate is not None) else True)
    # e.g. ResNet50-hp-l2-ppavg2-losscategorical_crossentropy-cs256-nofc-doc0.0-do0.0-dol0.0-poolingnone-cas-epoch008-val_acc0.575105.hdf5
    model_basename = os.path.splitext(os.path.basename(args.model))[0]
    model_parts = model_basename.split('-')
    model_name = '-'.join([part for part in model_parts if part not in ['epoch', 'val_acc']])
    args.classifier = model_parts[0]
    CROP_SIZE = args.crop_size  = model.get_input_shape_at(0)[1] if not args.triplet_loss else model.get_input_shape_at(0)[0][1]
    print("Overriding classifier: {} and crop size: {}".format(args.classifier, args.crop_size))
    last_epoch = int(list(filter(lambda x: x.startswith('epoch'), model_parts))[0][5:])
    print("Last epoch: {}".format(last_epoch))
    print("Model name: " + model_name)

    if args.learning_rate == None and training:
        dummy_model = model
        args.learning_rate = K.eval(model.optimizer.lr)
        print("Resuming with learning rate: {:.2e}".format(args.learning_rate))

elif True:

    if args.learning_rate is None:
        args.learning_rate = 1e-4   # default LR unless told otherwise

    last_epoch = 0

    classifier = globals()[args.classifier]

    kwargs = { \
        'include_top' : False,
        'weights'     : 'imagenet' if args.use_imagenet_weights else None,
        'input_shape' : (CROP_SIZE, CROP_SIZE, 3), 
        'pooling'     : args.pooling if args.pooling != 'none' else None,
     }

    classifier_args, _, _, _ = inspect.getargspec(classifier)

    if 'bottleneck_features' in classifier_args:
        kwargs['bottleneck_features'] = args.bottleneck_features

    classifier_model = classifier(**kwargs)

    for layer in args.delete_layers:
        classifier_model = delete_layer(classifier_model, classifier_model.get_layer(layer))

    trainable = False
    n_trainable = 0

    for i, layer in enumerate(classifier_model.layers):
        if (i >= args.freeze and args.freeze_until is None) or (layer.name == args.freeze_until):
            trainable = True
        if trainable:
            n_trainable += 1
        layer.trainable = trainable

    print("Base model has " + str(n_trainable) + "/" + str(len(classifier_model.layers)) + " trainable layers")

    if args.print_classifier_summary:
        classifier_model.summary()

    if args.triplet_loss:
        input_image_p1 = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image_p1' )
        input_image_p2 = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image_p2' )
        input_image_n1 = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image_n1' )

        x = concatenate([classifier_model(input_image_p1), classifier_model(input_image_p2), classifier_model(input_image_n1)])

        def triplet_loss(X):
            # https://arxiv.org/pdf/1804.07275v1.pdf
            # Eq (1)
            features = K.int_shape(X)[-1] // 3
            p1, p2, n1 = X[...,:features], X[...,features:2*features], X[...,2*features:]
            d_p1_p2 = K.sum(K.square(p1 - p2), axis=-1, keepdims=True)
            d_p1_n1 = K.sum(K.square(p1 - n1), axis=-1, keepdims=True)
            d_p2_n1 = K.sum(K.square(p2 - n1), axis=-1, keepdims=True)
            m = 2.

            loss = K.relu(m +  d_p1_p2 - d_p1_n1 ) + K.relu(m +  d_p1_p2 - d_p2_n1)
            
            # Eq (3,4) note: lambda trade-off param confirmed to be 1e-3 by the paper authors (by email)
            loss += 1e-3 * ( \
                K.sum(K.square(p1), axis=-1, keepdims=True) + \
                K.sum(K.square(p2), axis=-1, keepdims=True) + \
                K.sum(K.square(n1), axis=-1, keepdims=True))

            return loss

        loss = Lambda(triplet_loss, output_shape = (1,), name='triplet_loss')(x)
        
        model = Model(inputs=[input_image_p1, input_image_p2, input_image_n1], 
            outputs=loss)

    elif args.knn and (args.test_train or args.test):
        input_image = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image' )

        feature = classifier_model(input_image)
        
        model = Model(inputs=input_image, outputs=feature)

    else:
        input_image = Input(shape=(CROP_SIZE, CROP_SIZE, 3),  name = 'image' )
        x = input_image

        x = classifier_model(x)

        if args.reduce_pooling and x.shape.ndims == 4:
            # reduce feature channels after classifier using convs

            pool_features = int(x.shape[3])

            for it in range(int(math.log2(pool_features/args.reduce_pooling))):

                pool_features //= 2
                x = Conv2D(pool_features, (3, 3), padding='same', use_bias=False, name='reduce_pooling{}'.format(it))(x)
                x = BatchNormalization(name='bn_reduce_pooling{}'.format(it))(x)
                x = Activation('relu', name='relu_reduce_pooling{}'.format(it))(x)
            
        if x.shape.ndims > 2:
            # reduce spatial channels after classifier using pooling
            if args.post_pooling == 'avg':
                x = AveragePooling2D(pool_size=args.post_pool_size)(x)
            elif args.post_pooling == 'max':
                x = MaxPooling2D(pool_size=args.post_pool_size)(x)

            x = Reshape((-1,), name='reshape0')(x)

        if args.dropout_classifier != 0.:
            x = Dropout(args.dropout_classifier, name='dropout_classifier')(x)

        if not args.no_fcs and not args.hadamard:

            # regular FC classifier
            dropouts = np.linspace( args.dropout,  args.dropout_last, len(args.fully_connected_layers))

            x_m = x

            for i, (fc_layer, dropout) in enumerate(zip(args.fully_connected_layers, dropouts)):
                if args.batch_normalization:
                    x_m = Dense(fc_layer//2, name= 'fc_m{}'.format(i))(x_m)
                    x_m = BatchNormalization(name= 'bn_m{}'.format(i))(x_m)
                    x_m = Activation(args.fully_connected_activation, 
                                             name= 'act_m{}{}'.format(args.fully_connected_activation,i))(x_m)
                else:
                    x_m = Dense(fc_layer//2, activation=args.fully_connected_activation, 
                                             name= 'fc_m{}'.format(i))(x_m)
                if dropout != 0:
                    x_m = Dropout(dropout,   name= 'dropout_fc_m{}_{:04.2f}'.format(i, dropout))(x_m)

            for i, (fc_layer, dropout) in enumerate(zip(args.fully_connected_layers, dropouts)):
                if args.batch_normalization:
                    x = Dense(fc_layer,    name= 'fc{}'.format(i))(x)
                    x = BatchNormalization(name= 'bn{}'.format(i))(x)
                    x = Activation(args.fully_connected_activation, name='act{}{}'.format(args.fully_connected_activation,i))(x)
                else:
                    x = Dense(fc_layer, activation=args.fully_connected_activation, name= 'fc{}'.format(i))(x)
                if dropout != 0:
                    x = Dropout(dropout,                   name= 'dropout_fc{}_{:04.2f}'.format(i, dropout))(x)


        if args.hadamard:
            # ignore unscaled logits for now (_)
            x_features = x
            x, logits  = HadamardClassifier(N_CLASSES, name= "logits", l2_normalize=args.l2_normalize, output_raw_logits=True)(x)
        elif not args.no_dense:
            x          = Dense(             N_CLASSES, name= "logits")(x)

        #print("Using {} activation and {} loss for predictions". format(activation, args.loss))          

        prediction = Activation(activation ="softmax", name="predictions")(x)

        if args.include_distractors:
            if args.project_classifier_features != 0:
                d = HadamardClassifier(args.project_classifier_features, name= "features_project", l2_normalize=True, output_raw_logits=False)(x_features)
            else:    
                d = logits
            if args.top_k != 0:
                import tensorflow as tf
                def top_k(x, k):
                    v, _ = tf.nn.top_k(x, k)
                    return v
                d = Lambda(top_k, output_shape = (args.top_k,), arguments={'k' : args.top_k}, name='top_{}_logits'.format(args.top_k))(d)

            if args.vgg_places365:
                places_features = VGG16Places365(include_top=False, pooling='avg')(input_image)
                d = concatenate([d, places_features])
            elif args.vgg_places1365:
                places_features = VGG16PlacesHybrid1365(include_top=False, pooling='avg')(input_image)
                d = concatenate([d, places_features])
            for features in [1024,512,256,128]:
                    d = Dense(features,    name= 'd_fc{}'.format(features))(d)
                    d = BatchNormalization(name= 'bn_m{}'.format(features))(d)
                    d = Activation(args.fully_connected_activation,
                        name= 'act_m{}{}'.format(args.fully_connected_activation,features))(d)

            distractor = Dense(   1, activation='sigmoid', name='distractors')(d)        

        model = Model(inputs=input_image, outputs=prediction if not args.include_distractors else (prediction, distractor))

        if args.include_distractors:
            model.get_layer('logits').trainable = False

    model_name = args.classifier

    if args.triplet_loss:
        model_name += '-cs{}'.format(args.crop_size) 
    else:
        model_name += ('-hp' if args.hadamard else '') + \
        ('-l2' if args.l2_normalize else '-nol2' if args.hadamard else '') + \
        ('-pp{}{}'.format(args.post_pooling, args.post_pool_size) if args.post_pooling else '') + \
        '-loss{}'.format(args.loss) + \
        '-cs{}'.format(args.crop_size) + \
        ('-fc{}'.format(','.join([str(fc) for fc in args.fully_connected_layers])) if not args.no_fcs else '-nofc') + \
        ('-bn' if args.batch_normalization else '') + \
        (('-doc' + str(args.dropout_classifier)) if args.dropout_classifier != 0. else '') + \
        (('-do'  + str(args.dropout)) if args.dropout != 0. else '') + \
        (('-dol' + str(args.dropout_last)) if args.dropout_last != 0. else '') + \
        ('-pooling' + args.pooling) + \
        ('-id' if args.include_distractors else '') + \
        ('-vggplaces365' if args.vgg_places365 else '') + \
        ('-vggplaces1365' if args.vgg_places1365 else '') + \
        ('-pcf' + str(args.project_classifier_features) if args.project_classifier_features != 0 else '') + \
        (('-topk'  + str(args.top_k)) if args.top_k != 0. else '') + \
        ('-cas' if args.class_aware_sampling else '') + \
        ('-nd' if args.no_dense else '') + \
        ('-ps' if args.pavel_split else '') + \
        ('-top25' if args.top25 else '')

    print("Model name: " + model_name)

    if args.weights:
        print("Loading weights from {}".format(args.weights))
        model.load_weights(args.weights, by_name=True, skip_mismatch=True)
        match = re.search(r'([,A-Za-z_\d\.]+)-epoch(\d+)-.*\.hdf5', args.weights)
        last_epoch = int(match.group(2))        

if training:

    if not args.triplet_loss:
        if args.pavel_split:
            ids_train = [item for item in TRAIN_JPGS if get_id(item) not in pavel_ids]
            ids_val   = list(set(TRAIN_JPGS).difference(set(ids_train)))
        elif args.top25:
            TRAIN_JPGS = [item for item in TRAIN_JPGS if get_id(item) in top25_ids]
            ids_train, ids_val, _, _ = train_test_split(
                TRAIN_JPGS, TRAIN_CATS, test_size=args.val_percent, random_state=SEED, stratify=TRAIN_CATS)
        else:
            # split train/val using stratification
            ids_train, ids_val, _, _ = train_test_split(
                TRAIN_JPGS, TRAIN_CATS, test_size=args.val_percent, random_state=SEED, stratify=TRAIN_CATS)

        if args.remove_indoor:
            print("Before removing indoor images: Train split: {} Valid split {}".format(len(ids_train), len(ids_val)))
            INDOOR_IMAGES_URL = 'https://s3-us-west-2.amazonaws.com/kaggleglm/train_indoor.txt'
            INDOOR_IMAGES_PATH = get_file(
                'train_indoor.txt',
                INDOOR_IMAGES_URL,
                cache_subdir='models',
                file_hash='a0ddcbc7d0467ff48bf38000db97368e')
            indoor_images = set(open(INDOOR_IMAGES_PATH, 'r').read().splitlines())
            ids_train = [e for e in ids_train if str(e).split('/')[-1].split('.')[0] not in indoor_images]
            ids_val   = [e for e in ids_val   if str(e).split('/')[-1].split('.')[0] not in indoor_images]
            print("After removing indoor images:  Train split: {} Valid split {}".format(len(ids_train), len(ids_val)))

        if args.include_distractors:
            n_distractor_val_split = int(len(DISTRACTOR_JPGS) / 2)
            ids_val.extend(DISTRACTOR_JPGS[:n_distractor_val_split])
            ids_train.extend(DISTRACTOR_JPGS[n_distractor_val_split:])
            print('Using {:.2f}% distractor items in val split'.format(100. * n_distractor_val_split / len(ids_val)))
            print('Using {:.2f}% distractor items in train split'.format(100. * (len(DISTRACTOR_JPGS) - n_distractor_val_split) / len(ids_train)))
            random.shuffle(ids_train)
            random.shuffle(ids_val)
        
        print("Train split: {} Valid split {}".format(len(ids_train), len(ids_val)))
        print("Train/valid items overlap {}".format(len(set(ids_train).intersection(set(ids_val)))))
        print("Landmarks in train split {}".format(len({get_class(item) for item in ids_train})))
        print("Landmarks in valid split {}".format(len({get_class(item) for item in ids_val})))

        # compute class weight if not using class-aware sampling
        classes_train = [get_class(idx) for idx in ids_train]
        class_weight = class_weight.compute_class_weight('balanced', np.unique(classes_train), classes_train)
    else:
        ids_train = TRAIN_JPGS
        ids_val   = None

    if args.optimizer == 'adam':
        opt = Adam(lr=args.learning_rate, amsgrad=args.amsgrad)
    elif args.optimizer == 'sgd':
        opt = SGD(lr=args.learning_rate, decay=1e-6, momentum=0.9, nesterov=True)
    elif args.optimizer == 'adadelta':
        opt = Adadelta(lr=args.learning_rate, amsgrad=args.amsgrad)
    else:
        assert False

    if args.freeze_classifier:
        for layer in model.layers:
            if isinstance(layer, Model):
                print("Freezing weights for classifier {}".format(layer.name))
                for classifier_layer in layer.layers:
                    classifier_layer.trainable = False
                if not args.freeze_all_classifiers:
                    break # otherwise freeze only first

    if args.triplet_loss:
        loss = identity_loss
    else:
        if args.include_distractors:
            loss = { 'predictions' : zero_loss, 'distractors' : args.loss} 
        else:
            loss = { 'predictions' : args.loss} 

    model.summary()
    model = multi_gpu_model(model, gpus=args.gpus)

    model.compile(optimizer=opt, 
        loss=loss, 
        metrics={ 'predictions': ['categorical_accuracy'], 'distractors': ['binary_accuracy']} if not args.triplet_loss else None,
        )

    if not args.triplet_loss:
        mode = 'max'
        if not args.include_distractors:
            metric  = "-val_acc{val_categorical_accuracy:.6f}"
            monitor = "val_categorical_accuracy"
        else:
            metric  = "-val_acc{val_distractors_binary_accuracy:.4f}"
            monitor = "val_distractors_binary_accuracy"
    else:
        mode = 'min'
        metric = "-triplet_loss{loss:.6f}"
        monitor = 'loss'

    save_checkpoint = ModelCheckpoint(
            join(MODEL_FOLDER, model_name+"-epoch{epoch:03d}"+metric+".hdf5"),
            monitor=monitor,
            verbose=0,  save_best_only=True, save_weights_only=False, mode=mode, period=1)

    reduce_lr = ReduceLROnPlateau(monitor=monitor, factor=0.2, patience=5, min_lr=1e-9, epsilon = 0.00001, verbose=1, mode=mode)
    
    clr = CyclicLR(base_lr=args.learning_rate/4, max_lr=args.learning_rate,
                        step_size=int(math.ceil(len(ids_train)  / args.batch_size)) * 1, mode='exp_range',
                        gamma=0.99994)

    accuracy_callback = AccuracyReset(join(MODEL_FOLDER, model_name+"-epoch{epoch:03d}-group{group:03d}.hdf5"))
    callbacks = [save_checkpoint]

    if args.class_aware_sampling:
        callbacks.append(accuracy_callback)

    if args.cyclic_learning_rate:
        callbacks.append(clr)
    else:
        callbacks.append(reduce_lr)

    if args.triplet_loss and False:
        callbacks.append(MonitorDistance())
    
    # an epoch is just number of training samples, however if using class-aware sampling items are 
    # oversampled so one epoch does not see all distinct training items.
    model.fit_generator(
            generator        = gen(ids_train, args.batch_size, accuracy_callback = accuracy_callback),
            steps_per_epoch  = int(math.ceil((len(ids_train) if not args.triplet_loss else N_CLASSES) / args.batch_size)),
            validation_data  = gen(ids_val, args.batch_size, training = False) if not args.triplet_loss else None,
            validation_steps = int(math.ceil(len(ids_val) / args.batch_size))  if not args.triplet_loss else None,
            epochs = args.max_epoch,
            callbacks = callbacks,
            initial_epoch = last_epoch,
            class_weight={  'predictions': class_weight } \
                if ((not args.class_aware_sampling) and (not args.include_distractors) and (not args.triplet_loss)) else None)

elif args.test or args.test_train:

    if args.test:
        with open(TEST_CSV, 'r') as csvfile:
            reader = csv.reader(csvfile, delimiter=',', quotechar='|')
            next(reader)
            all_test_ids = [ ]
            for row in reader:
                all_test_ids.append(row[0][1:-1])

    if args.knn:

        features_dir = Path(args.features_dir) / "{}-cs{}".format(args.classifier,args.crop_size)

        os.makedirs(features_dir, exist_ok=True)

        # compute features for up to args.knn_landmark_samples images from each landmark
        model.summary()
        n_features = model.outputs[0].shape[1]

        model = multi_gpu_model(model, gpus=args.gpus)

        with Pool(min(args.batch_size, cpu_count())) as pool:
            process_item_func  = partial(process_item, predict = True)

            imgs = np.empty((args.batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)

            if args.test_train:

                for landmark in tqdm(range(N_CLASSES)):

                    batch_id = 0
                    batch_idx = [ ]

                    idxs = landmark_to_ids[landmark][:args.knn_landmark_samples]

                    features = np.empty((len(idxs), n_features), dtype=np.float32)

                    items = [Path(TRAIN_DIR) / (idx + '.jpg') for idx in idxs]

                    #print(items)
                    batch_results = pool.map(process_item_func, items)
                    #print(batch_results)

                    f = 0
                    for idx, (img, _, _) in zip(idxs, batch_results):

                        if img is not None:

                            imgs[batch_id,...] = img
                            #print(f, idx, batch_id)
                            #show_image(img)
                            batch_idx.append(idx)
                            batch_id += 1

                            if batch_id == args.batch_size:
                                features[f:f+batch_id,...] = model.predict(imgs[:batch_id])
                                f += batch_id
                                batch_id = 0
                                batch_idx = [ ]

                    # predict remaining items (if any)
                    if batch_id != 0:
                        features[f:f+batch_id,...] = model.predict(imgs[:batch_id])
                        f += batch_id

                    np.save(features_dir / str(landmark), features[:f])
                    #if landmark == 10:
                    #    break

            elif args.test:

                def predict_minibatch():
                    features = model.predict(imgs[:batch_id])
                    for i, (feature, _idx) in enumerate(zip(features, batch_idx)):
                        np.save(features_dir / _idx , feature)

                batch_id = 0
                batch_idx = [ ]

                for idxs in tqdm(
                    (all_test_ids[ii:ii+args.batch_size] for ii in range(0, len(all_test_ids), args.batch_size)), 
                    total=math.ceil(len(all_test_ids) / args.batch_size)):

                    items = [Path(TEST_DIR) / (idx + '.jpg') for idx in idxs]

                    batch_results = pool.map(process_item_func, items)

                    for idx, (img, _, _) in zip(idxs, batch_results):

                        if img is not None:

                            imgs[batch_id,...] = img
                            batch_idx.append(idx)
                            batch_id += 1

                            if batch_id == args.batch_size:
                                predict_minibatch()
                                batch_id = 0
                                batch_idx = [ ]

                # predict remaining items (if any)
                if batch_id != 0:
                    predict_minibatch()


    else:
        has_distractor_head = True if len(model.outputs) > 1 else False
        model = Model(inputs=model.input, outputs=model.outputs + [model.get_layer('logits').output[1]])

        model.summary()
        model = multi_gpu_model(model, gpus=args.gpus)

        csv_name  = Path('csv') / (os.path.splitext(os.path.basename(args.model if args.model else args.weights))[0] +
          ('_ssd' if args.scale_score_distractors else '') + 
          ('_test' if args.test else '_train') + '.csv')

        if args.test:
            all_ids  = all_test_ids
            jpgs_dir = TEST_DIR
        else:
            all_ids  = list(TRAIN_IDS)#[:20000] # CHANGE
            jpgs_dir = TRAIN_DIR

        with Pool(min(args.batch_size, cpu_count())) as pool:
            process_item_func  = partial(process_item, predict = True)

            with open(csv_name, 'w') as csvfile:

                csv_writer = csv.writer(csvfile, delimiter=',',quotechar='|', quoting=csv.QUOTE_MINIMAL)
                csv_writer.writerow(['id','landmarks'])

                imgs = np.empty((args.batch_size, CROP_SIZE, CROP_SIZE, 3), dtype=np.float32)

                batch_id = 0
                batch_idx = [ ]

                def predict_minibatch():
                    if has_distractor_head:
                        predictions, distractors, logits = model.predict(imgs[:batch_id])
                    else:
                        predictions, logits              = model.predict(imgs[:batch_id])
                        distractors = predictions # hack to avoid code dup

                    cats = np.argmax(predictions, axis=1)
                    for i, (cat, distractor, logit, _idx) in enumerate(zip(cats, distractors, logits, batch_idx)):
                        #score = np.max(logit)
                        score = predictions[i, cat]
                        distractor = distractor[0]
                        landmark = cat_to_landmark[cat]
                        is_distractor = False
                        if has_distractor_head:
                            if not args.scale_score_distractors:
                                if  distractor >= 0.5:
                                    #landmark = -1 
                                    score -= 10000.
                            else:
                                score *= (1. - distractor)
                        if (score >= args.threshold):
                            csv_writer.writerow([_idx, "{} {}".format(landmark, score)])
                        else:
                            csv_writer.writerow([_idx, ""])    

                for idxs in tqdm(
                    (all_ids[ii:ii+args.batch_size] for ii in range(0, len(all_ids), args.batch_size)), 
                    total=math.ceil(len(all_ids) / args.batch_size)):

                    items = [Path(jpgs_dir) / (idx + '.jpg') for idx in idxs]

                    batch_results = pool.map(process_item_func, items)

                    for idx, (img, _, _) in zip(idxs, batch_results):

                        if img is not None:

                            imgs[batch_id,...] = img
                            batch_idx.append(idx)
                            batch_id += 1

                            if batch_id == args.batch_size:
                                predict_minibatch()
                                batch_id = 0
                                batch_idx = [ ]
                        else:
                            csv_writer.writerow([idx, ""])

                # predict remaining items (if any)
                if batch_id != 0:
                    predict_minibatch()

        if args.test:
            print("kaggle competitions submit -f {} -m '{}'".format(
                csv_name,
                ' '.join(sys.argv)
                ))


