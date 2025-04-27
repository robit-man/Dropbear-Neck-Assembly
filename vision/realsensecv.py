#!/usr/bin/env python3
"""
RealSense D455 capture helper  — v3
• Streams colour, refined depth (white-near → black-far), and both IR imagers
• Applies on-device post-processing filters:
    – Decimation  (down-samples & removes bad pixels)
    – Spatial     (edge-preserving smoothing)
    – Temporal    (temporal smoothing)
    – Hole-filling
    – Disparity-domain trick (as recommended by Intel)
Public API is unchanged:  start() · read() · release()
"""

import pyrealsense2 as rs
import numpy as np
import cv2


class RealsenseCapture:
    # ---------- stream parameters ----------
    WIDTH, HEIGHT, FPS       = 1280, 720, 15               # depth + IR
    WIDTH_C, HEIGHT_C, FPS_C = 1280, 800, 30               # colour
    MAX_DEPTH_MM             = 8_000                       # clip at 8 m for gradient

    def __init__(self):
        # ---------- pipeline & streams ----------
        self.config = rs.config()

        self.config.enable_stream(
            rs.stream.color,
            self.WIDTH_C, self.HEIGHT_C,
            rs.format.bgr8, self.FPS_C
        )

        self.config.enable_stream(
            rs.stream.depth,
            self.WIDTH, self.HEIGHT,
            rs.format.z16, self.FPS
        )

        for idx in (1, 2):                                 # infrared L / R
            self.config.enable_stream(
                rs.stream.infrared, idx,
                self.WIDTH, self.HEIGHT,
                rs.format.y8, self.FPS
            )

        # ---------- post-processing filters ----------
        self.decimate             = rs.decimation_filter()
        self.decimate.set_option(rs.option.filter_magnitude, 2)   # 2× reduction (optional)

        self.depth_to_disparity   = rs.disparity_transform(True)
        self.disparity_to_depth   = rs.disparity_transform(False)

        self.spatial              = rs.spatial_filter()
        self.spatial.set_option(rs.option.holes_fill, 2)          # light hole-filling

        self.temporal             = rs.temporal_filter()

        self.hole_filling         = rs.hole_filling_filter()

        self.pipeline = None

    # ---------- lifecycle ----------
    def start(self):
        self.pipeline = rs.pipeline()
        self.pipeline.start(self.config)
        print("RealSense pipeline started with smoothing filters.")

    def release(self):
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
            print("RealSense pipeline stopped.")

    # ---------- helpers ----------
    def _process_depth(self, depth_frame: rs.frame) -> rs.frame:
        """Apply the librealsense recommended filter chain."""
        f = self.decimate.process(depth_frame)
        f = self.depth_to_disparity.process(f)
        f = self.spatial.process(f)
        f = self.temporal.process(f)
        f = self.disparity_to_depth.process(f)
        f = self.hole_filling.process(f)
        return f

    @staticmethod
    def _to_numpy(frame: rs.video_frame) -> np.ndarray:
        return np.asanyarray(frame.get_data())

    # ---------- frame grab ----------
    def read(self, *, as_numpy: bool = True, include_ir: bool = False):
        """
        Returns
        -------
        ret    : bool
        frames : tuple
          (colour, depth_vis)           – if include_ir=False
          (colour, depth_vis, irL, irR) – if include_ir=True
        """
        if self.pipeline is None:
            raise RuntimeError("start() must be called before read()")

        fs = self.pipeline.wait_for_frames()

        color_f = fs.get_color_frame()
        depth_f_raw = fs.get_depth_frame()
        if include_ir:
            ir_l_f = fs.get_infrared_frame(1)
            ir_r_f = fs.get_infrared_frame(2)
        else:
            ir_l_f = ir_r_f = None

        if not color_f or not depth_f_raw or (include_ir and (not ir_l_f or not ir_r_f)):
            return False, (None,) * (4 if include_ir else 2)

        # ---------- depth post-processing ----------
        depth_f = self._process_depth(depth_f_raw)

        if not as_numpy:
            base = (color_f, depth_f)
            return True, base + (ir_l_f, ir_r_f) if include_ir else base

        # ---> NumPy conversions
        color_img = self._to_numpy(color_f)

        depth_mm = self._to_numpy(depth_f).astype(np.uint16)
        depth_mm_clipped = np.clip(depth_mm, 0, self.MAX_DEPTH_MM)
        depth8 = cv2.convertScaleAbs(depth_mm_clipped,
                                     alpha=255.0 / self.MAX_DEPTH_MM,
                                     beta=0)
        depth_inv = 255 - depth8                         # white near / black far
        depth_vis = cv2.cvtColor(depth_inv, cv2.COLOR_GRAY2BGR)

        if not include_ir:
            return True, (color_img, depth_vis)

        ir_left  = self._to_numpy(ir_l_f)
        ir_right = self._to_numpy(ir_r_f)
        return True, (color_img, depth_vis, ir_left, ir_right)
