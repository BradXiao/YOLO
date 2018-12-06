import copy
import cv2
import threading
import os
import sys

import rospy
from std_msgs.msg import Float32MultiArray, MultiArrayDimension
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError

import mxnet
from mxnet import gpu
from mxnet import nd

from yolo_modules import yolo_cv
from yolo_modules import yolo_gluon
from yolo_modules import licence_plate_render
from yolo_modules import global_variable

from utils import *

if sys.argv[1] == 'v2':
    print(global_variable.cyan)
    print('load car_and_LP v2')
    from car.YOLO import *

else:
    from YOLO import *


def main():
    args = video_Parser()
    video = Video(args)
    video()


class Video():
    def __init__(self, args):

        self.yolo = YOLO(args)

        self.yolo.car_threshold = 0.6
        self.yolo.LP_threshold = 0.9

        self.project_rect_6d = licence_plate_render.ProjectRectangle6D(
            int(380*1.05), int(160*1.05))
        self._init_ros()

        self.dev = args.dev
        self.topic = args.topic
        self.show = args.show
        self.radar = args.radar
        self.LP = args.LP
        self.car = args.car
        self.flip = args.flip
        self.clip = (args.clip_h, args.clip_w)
        self.ctx = yolo_gluon.get_ctx(args.gpu)

        size = tuple(self.yolo.size[::-1])
        self.mx_resize = mxnet.image.ForceResizeAug(size)

        if self.radar:
            self.radar_prob = yolo_cv.RadarProb(
                self.yolo.num_class, self.yolo.classes)

        print(global_variable.cyan)
        if self.dev == 'ros':
            rospy.Subscriber(self.topic, Image, self._image_callback)
            print('Image Topic: %s' % self.topic)

        else:
            threading.Thread(target=self._get_frame).start()

        threading.Thread(target=self._net_thread).start()
        save_frame = False
        if save_frame:
            fourcc = cv2.VideoWriter_fourcc(*'MP4V')  # (*'MJPG')#(*'MPEG')
            self.out = cv2.VideoWriter(
                './video/car_rotate.mp4', fourcc, 30, (640, 360))

    def __call__(self):
        yolo_gluon.test_inference_rate(
            self.yolo.net,
            (1, 3, self.yolo.size[0], self.yolo.size[1]))

        rate = rospy.Rate(30)

        while not hasattr(self, 'net_out') or not hasattr(self, 'net_img'):
            rate.sleep()

        while not rospy.is_shutdown():
            net_out = copy.copy(self.net_out)  # not sure type(net_out)
            img = self.net_img.copy()
            pred = self.yolo.predict(net_out[:5], net_out[5:])

            ros_publish_array(self.car_pub, self.mat1, pred[0][0])
            ros_publish_array(self.LP_pub, self.mat2, pred[1][0])
            self.visualize(pred)

            rate.sleep()

    def _init_ros(self):
        rospy.init_node("YOLO_ros_node", anonymous=True)
        self.bridge = CvBridge()

        self.img_pub = rospy.Publisher(
            self.yolo.pub_img,
            Image,
            queue_size=1)

        self.clipped_LP_pub = rospy.Publisher(
            self.yolo.pub_clipped_LP,
            Image,
            queue_size=1)

        self.car_pub = rospy.Publisher(
            self.yolo.pub_box,
            Float32MultiArray,
            queue_size=1)

        self.LP_pub = rospy.Publisher(
            self.yolo.pub_LP,
            Float32MultiArray,
            queue_size=1)

        self.topk = 1

        self.mat1 = Float32MultiArray()
        dim = self.mat1.layout.dim
        dim.append(MultiArrayDimension())
        dim.append(MultiArrayDimension())
        dim[0].label = "box"
        dim[0].size = self.topk
        dim[0].stride = self.topk * 7

        dim[1].label = "predict"
        dim[1].size = 7
        dim[1].stride = 7

        self.mat2 = Float32MultiArray()
        dim = self.mat2.layout.dim
        dim.append(MultiArrayDimension())
        dim.append(MultiArrayDimension())
        dim[0].label = "LP"
        dim[0].size = self.topk
        dim[0].stride = self.topk * 8

        dim[1].label = "predict"
        dim[1].size = 8
        dim[1].stride = 8

    def cv2_flip_and_clip_frame(self, img):
        h, w = self.yolo.size

        clip = self.clip
        assert type(clip) == tuple and len(clip) == 2, (
            global_variable.red +
            'clip should be a tuple, (height_ratio, width_ratio')
        if clip[0] < 1:
            top = int((1-clip[0]) * img.shape[0] / 2.)
            bot = img.shape[0] - top
            img = img[top:bot]

        if clip[1] < 1:
            left = int((1-clip[1]) * img.shape[1] / 2.)
            right = img.shape[1] - left
            img = img[:, left:right]

        flip = self.flip
        if flip == 1 or flip == 0 or flip == -1:
            img = cv2.flip(img, flip)
            # flip = 1: left-right
            # flip = 0: top-down
            # flip = -1: 1 && 0

        img = cv2.resize(img, (w, h))
        return img

    def _get_frame(self):
        dev = self.dev
        pause = 0

        if dev == 'tx2' or dev == 'xavier':
            print('Image Source: Jetson OnBoard Camera')
            cap = open_cam_onboard(640, 360, dev)

        elif dev.split('.')[-1] in ['mp4', 'avi', '']:
            print('Image Source: ' + dev)
            cap = cv2.VideoCapture(dev)
            pause = 0.03

        elif dev.isdigit() and os.path.exists('/dev/video' + dev):
            print('Image Source: /dev/video' + dev)
            cap = cv2.VideoCapture(int(dev))

        else:
            print(global_variable.red)
            print(('dev should be (tx2) or (xavier) or '
                   '(video_path) or (device_index)'))
            sys.exit(0)

        while not rospy.is_shutdown():
            self.ret, img = cap.read()
            self.img = self.cv2_flip_and_clip_frame(img)
            if bool(pause):
                time.sleep(pause)

        cap.release()

    def _net_thread(self):
        while not rospy.is_shutdown():
            if not hasattr(self, 'img') or self.img is None:
                print('Wait For Image')
                time.sleep(1.0)
                continue

            #self.lock.acquire()
            self.net_img = self.img.copy()
            nd_img = yolo_gluon.cv_img_2_ndarray(
                self.net_img, self.ctx[0], mxnet_resize=self.mx_resize)

            self.net_out = self.yolo.net.forward(is_train=False, data=nd_img)
            #self.lock.release()

            self.net_out[0].wait_to_read()

    def _image_callback(self, img):
        img = self.bridge.imgmsg_to_cv2(img, "bgr8")
        self.img = self.cv2_flip_and_clip_frame(img)

    def visualize(self, out):
        Cout = out[0][0]
        LP_out = out[1][0]
        img = copy.deepcopy(self.img)
        #self.out.write(img)
        if self.radar:
            self.radar_prob.plot3d(
                Cout[0], Cout[-self.yolo.num_class:])

        # -------------------- Licence Plate -------------------- #
        if LP_out[0] > self.yolo.LP_threshold and self.LP:
            img, clipped_LP = self.project_rect_6d.add_edges(img, LP_out[1:])
            self.clipped_LP_pub.publish(
                self.bridge.cv2_to_imgmsg(clipped_LP, 'bgr8'))

            if self.show:
                cv2.imshow('Licence Plate', clipped_LP)

        # -------------------- vehicle -------------------- #
        if Cout[0] > self.yolo.car_threshold and self.car:
            yolo_cv.cv2_add_bbox(img, Cout, 4, use_r=False)

        if self.show:
            cv2.imshow('img', img)
            cv2.waitKey(1)

        self.img_pub.publish(self.bridge.cv2_to_imgmsg(img, 'bgr8'))


def ros_publish_array(ros_publisher, mat, data):
    # self.mat1.data = [-1] * 7
    # self.mat2.data = [-1] * 8
    mat.data = data
    ros_publisher.publish(mat)


if __name__ == '__main__':
    main()
