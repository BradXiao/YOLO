import os
import glob
import PIL

import mxnet
from mxnet import nd

from yolo_modules import global_variable


def get_latest_weight_from(path):
    backup_list = glob.glob(path + '/*')
    if len(backup_list) != 0:
        weight = max(backup_list, key=os.path.getctime)
        print('Find latest weight: %s' % weight)

    else:
        weight = 'No pretrain weight'

    return weight


def init_NN(target, weight, ctx):
    print(global_variable.yellow)
    print('use pretrain weight: %s' % weight)
    try:
        target.collect_params().load(weight, ctx=ctx)
        print(global_variable.green)
        print('Load Pretrain Successfully')

    except Exception as e:
        print(global_variable.red)
        print('Load Pretrain Failed, Use Xavier initializer')
        print(e.message.split('\n')[0])
        target.initialize(init=mxnet.init.Xavier(), ctx=ctx)

    finally:
        target.hybridize()


def pil_mask_2_rgb_ndarray(m):
    m = nd.array(m).reshape(1, m.size[1], m.size[0])
    return nd.tile(m, (3, 1, 1)) / 255.


def pil_rgb_2_rgb_ndarray(pil_img, augs=None):
    pil_img = PIL.Image.merge("RGB", (pil_img.split()[:3]))
    img = nd.array(pil_img)

    if augs is not None:
        for aug in augs:
            img = aug(img)

    return img.transpose((2, 0, 1)) / 255.


def nd_inv_sigmoid(x):

    return -nd.log(1/x - 1)


def batch_ndimg_2_cv2img(x):

    return x.transpose((0, 2, 3, 1)).asnumpy()


def load_background(train_or_val, bs, h, w, **kargs):
    path = global_variable.training_data_path
    path = path + '/HP_31/sun2012_' + train_or_val
    if train_or_val == 'train':
        shuffle = True
    elif train_or_val == 'val':
        shuffle = False

    BG_iter = mxnet.image.ImageIter(
        bs, (3, h, w),
        path_imgrec=path + '.rec',
        path_imgidx=path + '.idx',
        shuffle=shuffle,
        pca_noise=0,
        brightness=0.5, saturation=0.5, contrast=0.5, hue=1.0,
        rand_crop=True, rand_resize=True, rand_mirror=True, inter_method=10)

    BG_iter.reset()
    return BG_iter


def split_render_data(batch, ctx):
    # split_render_data(batch_x, batch_y, ctx)
    # >>> split_render_data(batch, ctx)
    # splitted_batches = [data0 in gpu0, data0 in gpu1]
    splitted_batch = []
    batch_size = len(batch)

    for i, dev in enumerate(ctx):
        start = int(i*batch_size/len(ctx))
        end = int((i+1)*batch_size/len(ctx))

        batch_at_gpu_i = batch[start:end].as_in_context(dev)
        splitted_batch.append(batch_at_gpu_i)

    return splitted_batch


def ImageIter_next_batch(BG_iter):
    try:
        return BG_iter.next().data[0]
    except:
        BG_iter.reset()
        return BG_iter.next().data[0]


def assign_batch(batch, ctx):
    if len(ctx) > 1:
        batch_xs = mxnet.gluon.utils.split_and_load(batch.data[0], ctx)
        batch_ys = mxnet.gluon.utils.split_and_load(batch.label[0], ctx)
    else:
        batch_xs = [batch.data[0].as_in_context(ctx[0])] # b*RGB*w*h
        batch_ys = [batch.label[0].as_in_context(ctx[0])] # b*L*5   
    return batch_xs, batch_ys


def get_iterators(
    data_root, file_name, data_shape, batch_size,
    brightness=0.2, contrast=0.2, saturation=0.5, hue=1.0,
    rand_crop=0, rand_pad=0, area_range=(0.8, 1.2)
        ):

    print('\033[1;33;40m')
    print('Loading Data Iterators....')
    print('Data path: {}'.format(os.path.join(data_root, file_name)))
    print('Batch Size: {}'.format(batch_size))
    print('Data shape: {}'.format(data_shape))
    print('Brightness: {}, Contrast: {}, Saturation: {}, Hue:{}'.format(
        brightness, contrast, saturation, hue))
    print('Rand Crop={}, Rand Pad={}, Area Range={}'.format(
        rand_crop, rand_pad, area_range))
    print('\033[0m')

    batch_iter = mxnet.image.ImageDetIter(
        batch_size=batch_size,
        data_shape=(3, data_shape[0], data_shape[1]),
        path_imgrec=os.path.join(data_root, file_name+'.rec'),
        path_imgidx=os.path.join(data_root, file_name+'.idx'),
        shuffle=True,
        brightness=brightness,
        contrast=contrast,
        saturation=saturation,
        hue=hue,
        rand_crop=rand_crop,
        rand_pad=rand_pad,
        area_range=area_range,
        )

    return batch_iter


def get_iou(predict, target, mode=1):
    '''
    Parameter:
    ----------
    predict: mxnet.ndarray
      channels are {???}*4
    target: mxnet.ndarray
      target.shape = (5)
    mode: [1,2]
      1: target format is cltrb
      2: target fromat is cyxhw

    Returns
    ----------
    ious: mxnet.ndarray
      ious between predict and target, dimasion is {???}x1
    '''
    l, t, r, b = predict.split(num_outputs=4, axis=-1)
    if mode == 1:
        l2 = target[1]
        t2 = target[2]
        r2 = target[3]
        b2 = target[4]
    elif mode == 2:
        l2 = target[2] - target[4]/2
        t2 = target[1] - target[3]/2
        r2 = target[2] + target[4]/2
        b2 = target[1] + target[3]/2
    else:
        print('mode should be int 1 or 2')

    i_left = nd.maximum(l2, l)
    i_top = nd.maximum(t2, t)
    i_right = nd.minimum(r2, r)
    i_bottom = nd.minimum(b2, b)
    iw = nd.maximum(i_right - i_left, 0.)
    ih = nd.maximum(i_bottom - i_top, 0.)
    inters = iw * ih
    predict_area = (r-l)*(b-t)
    target_area = target[3] * target[4]
    ious = inters/(predict_area + target_area - inters)
    return ious  # 1344x3x1


def nd_label_batch_ltrb2yxhw(label_batch):
    new_label_batch = nd.zeros_like(label_batch)

    new_label_batch[:, :, 0] = (label_batch[:, :, 1] + label_batch[:, :, 3])/2  # y
    new_label_batch[:, :, 1] = (label_batch[:, :, 0] + label_batch[:, :, 2])/2  # x
    new_label_batch[:, :, 2] = label_batch[:, :, 3] - label_batch[:, :, 1]  # h
    new_label_batch[:, :, 3] = label_batch[:, :, 2] - label_batch[:, :, 0]  # w

    return new_label_batch


'''
def load_ImageDetIter(path, batch_size, h, w):
    print('Loading ImageDetIter ' + path)
    batch_iter = mxnet.image.ImageDetIter(batch_size, (3, h, w),
        path_imgrec=path+'.rec',
        path_imgidx=path+'.idx',
        shuffle=True,
        pca_noise=0.1,
        brightness=0.5,
        saturation=0.5,
        contrast=0.5,
        hue=1.0
        #rand_crop=0.2,
        #rand_pad=0.2,
        #area_range=(0.8, 1.2),
        )
    return batch_iter
'''
