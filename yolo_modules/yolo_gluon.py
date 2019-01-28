import glob
import numpy
import os
import time
import PIL
import numpy

import mxnet
from mxnet import nd, gpu

from yolo_modules import global_variable


# -------------------- train/valid -------------------- #
def record_loss(losses, loss_names, summary_writer, step=0, exp=''):
    for i, L in enumerate(losses):
        loss_name = loss_names[i]
        summary_writer.add_scalar(
            exp,
            (loss_name, nd.mean(L).asnumpy()),
            step)


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


def ImageIter_next_batch(BG_iter):
    try:
        return BG_iter.next().data[0]
    except Exception as e:
        print(e)
        BG_iter.reset()
        return BG_iter.next().data[0]


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


# -------------------- net -------------------- #
def init_NN(target, weight, ctx):
    print(global_variable.magenta)
    print('use pretrain weight: %s' % weight)
    try:
        target.collect_params().load(weight, ctx=ctx)
        # print(global_variable.green)
        print('Load Pretrain Successfully')

    except Exception as e:
        print(global_variable.red)
        print('Load Pretrain Failed, Use Xavier initializer')
        print(e.message.split('\n')[0])
        target.initialize(init=mxnet.init.Xavier(), ctx=ctx)

    finally:
        target.hybridize()


def init_executor(export_folder, size, ctx, use_tensor_rt=False, step=0, fp16=False):
    print('checkpoint folder: %s' % export_folder)
    export_file = os.path.join(export_folder, 'export')
    sym, arg_params, aux_params = mxnet.model.load_checkpoint(
        export_file, step)
    shape = (1, 3, size[0], size[1])

    if fp16:
        type_dict = {'data': numpy.float16}
    else:
        type_dict = {'data': numpy.float32}

    if use_tensor_rt:
        print('Building TensorRT engine')
        os.environ['MXNET_USE_TENSORRT'] = '1'

        arg_params.update(aux_params)
        all_params = dict(
            [(k, v.as_in_context(ctx)) for k, v in arg_params.items()])

        executor = mxnet.contrib.tensorrt.tensorrt_bind(
            sym,
            all_params=all_params,
            ctx=ctx,
            data=shape,
            type_dict=type_dict,
            grad_req='null',
            force_rebind=True)

    else:
        executor = sym.simple_bind(
            ctx=ctx,
            data=shape,
            type_dict=type_dict,
            grad_req='null',
            force_rebind=True)
        executor.copy_params_from(arg_params, aux_params)

    return executor


def export(net, batch_shape, ctx, export_folder, onnx=False, epoch=0, fp16=False):
    data = nd.zeros(batch_shape).as_in_context(ctx)

    if fp16:
        data = data.astype('float16')

    net.forward(data)

    print(global_variable.yellow)
    print('export model to: %s' % export_folder)
    if not os.path.exists(export_folder):
        os.makedirs(export_folder)
    net.export(export_folder + '/export', epoch=epoch)

    if onnx:
        path = os.path.join(export_folder, 'onnx')
        if not os.path.exists(path):
            os.makedirs(path)

        onnx_file = os.path.join(path, 'out.onnx')
        print('export onnx to: %s' % onnx_file)
        sym = export_folder + '/export-symbol.json'
        params = export_folder + '/export-%04d.params' % epoch
        mxnet.contrib.onnx.export_model(
            sym, params, [batch_shape], numpy.float32, onnx_file)

    print(global_variable.green)
    print('Export Done')


def get_latest_weight_from(path):
    backup_list = glob.glob(path + '/*')
    if len(backup_list) != 0:
        weight = max(backup_list, key=os.path.getctime)
        print('Find latest weight: %s' % weight)

    else:
        weight = 'No pretrain weight'

    return weight


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


# -------------------- video -------------------- #
def nd_white_balance(nd_img, bgr=(1.0, 1.0, 1.0)):
    assert len(bgr) == 3 and nd_img.shape[1] == 3, (
        global_variable.red + 'len(bgr) != 3 or nd_img.shape[1] != 3')

    for i, color_weight in enumerate(bgr):
        nd_img[:, i, :, :] = nd_img[:, i, :, :] * color_weight

    return nd_img


def test_inference_rate(net, shape, cycles=100, ctx=mxnet.gpu(0)):
    # shape =  (1, 3, size[0], size[1])
    data = nd.zeros(shape).as_in_context(ctx)
    for _ in range(10):
        x = net.forward(is_train=False, data=data)
        x[0].wait_to_read()

    t = time.time()
    for _ in range(cycles):
        x1 = net.forward(is_train=False, data=data)
        x1[0].wait_to_read()

    print(global_variable.yellow)
    print('Inference Rate = %.2f' % (cycles/float(time.time() - t)))
    print(global_variable.reset_color)


# -------------------- other -------------------- #
def cv_img_2_ndarray(image, ctx, mxnet_resize=None):
    nd_img = nd.array(image)
    if mxnet_resize is not None:
        nd_img = mxnet_resize(nd_img)

    nd_img = nd_img.as_in_context(ctx)
    nd_img = nd_img.transpose((2, 0, 1)).expand_dims(axis=0) / 255.

    return nd_img


def batch_ndimg_2_cv2img(x):

    return x.transpose((0, 2, 3, 1)).asnumpy()


def nd_inv_sigmoid(x):

    return -nd.log(1/x - 1)


def get_ctx(gpu):

    return [mxnet.gpu(int(i)) for i in gpu]


def nd_label_batch_ltrb2yxhw(label_batch):
    new_label_batch = nd.zeros_like(label_batch)
    # x, y, h, w
    new_label_batch[:, :, 0] = (label_batch[:, :, 1] + label_batch[:, :, 3])/2
    new_label_batch[:, :, 1] = (label_batch[:, :, 0] + label_batch[:, :, 2])/2
    new_label_batch[:, :, 2] = label_batch[:, :, 3] - label_batch[:, :, 1]
    new_label_batch[:, :, 3] = label_batch[:, :, 2] - label_batch[:, :, 0]

    return new_label_batch
