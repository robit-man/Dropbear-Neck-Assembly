import pyrealsense2 as rs
import numpy as np


class RealsenseCapture:

    def __init__(self):
        self.WIDTH = 1280
        self.HEIGHT = 720
        self.FPS = 15
        self.WIDTH_C = 1280
        self.HEIGHT_C = 800
        self.FPS_C = 30
        # Configure depth and color streams
        self.config = rs.config()
        self.config.enable_stream(rs.stream.color, self.WIDTH_C, self.HEIGHT_C, rs.format.bgr8, self.FPS_C)
        self.config.enable_stream(rs.stream.depth, self.WIDTH, self.HEIGHT, rs.format.z16, self.FPS)

    def start(self):
        # Start streaming
        self.pipeline = rs.pipeline()
        self.pipeline.start(self.config)
        print('pipline start')

    def read(self, is_array=True):
        # Flag capture available
        ret = True
        # get frames
        frames = self.pipeline.wait_for_frames()
        # separate RGB and Depth image
        self.color_frame = frames.get_color_frame()  # RGB
        self.depth_frame = frames.get_depth_frame()  # Depth

        if not self.color_frame or not self.depth_frame:
            ret = False
            return ret, (None, None)
        elif is_array:
            # Convert images to numpy arrays
            color_image = np.array(self.color_frame.get_data())
            depth_image = np.array(self.depth_frame.get_data())
            return ret, (color_image, depth_image)
        else:
            return ret, (self.color_frame, self.depth_frame)

    def release(self):
        # Stop streaming
        self.pipeline.stop()

