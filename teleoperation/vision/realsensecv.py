#!/usr/bin/env python3
"""
RealSense D455 capture helper – v6
──────────────────────────
Goal: keep very-close objects pure white, *and* retain far-field contrast.

Strategy
========
• Always pin the near end of the gradient to 0.30 m (≈ minimum useful range).
• Dynamically choose the far end from the 99-percentile of **valid** pixels,
  but never above 15 m.  →  Scene-adaptive stretch that still preserves
  distant detail instead of collapsing it into solid black.
• Optional gamma (=0.6) brightens the dark end so fine far details show up.
• Same filter stack for accuracy.

Public API unchanged:  start() · read() · release()
"""

import time
import pyrealsense2 as rs
import numpy as np
import cv2


class RealsenseCapture:
    # ---------- stream parameters ----------
    WIDTH, HEIGHT, FPS       = 1280, 720, 15
    WIDTH_C, HEIGHT_C, FPS_C = 1280, 800, 30

    # ---------- gradient settings ----------
    MIN_DEPTH_M   = 0.01      # pure white
    MAX_DEPTH_CAP = 15.0      # never map beyond this
    GAMMA         = 1         # <1 boosts far-end contrast; set to 1.0 for linear

    def __init__(self, stream_ir=True):
        self.stream_ir = bool(stream_ir)
        self.config = rs.config()
        self.config.enable_stream(rs.stream.color,
                                  self.WIDTH_C, self.HEIGHT_C,
                                  rs.format.bgr8, self.FPS_C)
        self.config.enable_stream(rs.stream.depth,
                                  self.WIDTH, self.HEIGHT,
                                  rs.format.z16, self.FPS)
        if self.stream_ir:
            for i in (1, 2):
                self.config.enable_stream(rs.stream.infrared, i,
                                          self.WIDTH, self.HEIGHT,
                                          rs.format.y8, self.FPS)

        # --- enable IMU (accel + gyro) ---
        self.config.enable_stream(rs.stream.accel,
                                  rs.format.motion_xyz32f, 250)
        self.config.enable_stream(rs.stream.gyro,
                                  rs.format.motion_xyz32f, 200)

        # --------- filter chain ---------
        self.decimate   = rs.decimation_filter()
        self.decimate.set_option(rs.option.filter_magnitude, 2)

        self.d2disp     = rs.disparity_transform(True)
        self.spatial    = rs.spatial_filter();  self.spatial.set_option(rs.option.holes_fill, 2)
        self.temporal   = rs.temporal_filter()
        self.disp2d     = rs.disparity_transform(False)
        self.hole_fill  = rs.hole_filling_filter()

        self.pipeline   = None
        self.depth_scale = None

    # --------- lifecycle ---------
    def start(self, max_retries=None, initial_backoff=1.0, max_backoff=30.0):
        self.pipeline = rs.pipeline()
        backoff = max(0.1, float(initial_backoff))
        max_backoff = max(backoff, float(max_backoff))
        attempts = 0

        # Retry with exponential backoff. If max_retries is set, raise after limit.
        while True:
            attempts += 1
            try:
                prof = self.pipeline.start(self.config)
                break
            except Exception as e:
                try:
                    self.pipeline.stop()
                except Exception:
                    pass
                self.pipeline = rs.pipeline()
                if max_retries is not None and attempts >= int(max_retries):
                    raise RuntimeError(
                        f"RealSense start() failed after {attempts} attempts: {e!r}"
                    ) from e
                print(f"[RealSense] start() failed: {e!r}, retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        sensor = prof.get_device().first_depth_sensor()
        self.depth_scale = sensor.get_depth_scale()
        print(f"[RealSense] depth_scale = {self.depth_scale:.6f} m/unit")

        # try long-range preset (ignore errors)
        try:
            sensor.set_option(rs.option.visual_preset, rs.rs400_visual_preset.long_range)
        except Exception:
            pass

    def release(self):
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
            print("[RealSense] pipeline stopped.")

    # --------- helpers ---------
    def _filter_depth(self, df):
        f = self.decimate.process(df)
        f = self.d2disp.process(f)
        f = self.spatial.process(f)
        f = self.temporal.process(f)
        f = self.disp2d.process(f)
        f = self.hole_fill.process(f)
        return f

    @staticmethod
    def _np(frame): return np.asanyarray(frame.get_data())

    def _depth_vis(self, depth_f):
        depth_m = self._np(depth_f).astype(np.float32) * self.depth_scale
        valid   = depth_m[(depth_m > 0) & (depth_m < self.MAX_DEPTH_CAP)]

        if valid.size < 100:
            return np.zeros((*depth_m.shape, 3), dtype=np.uint8)

        near = self.MIN_DEPTH_M
        far  = np.percentile(valid, 99)              # dynamic far
        far  = max(far, near + 0.1)                  # avoid zero span
        far  = min(far, self.MAX_DEPTH_CAP)

        span = far - near
        norm = np.clip((depth_m - near) / span, 0, 1)
        inv  = 1.0 - norm
        if self.GAMMA != 1.0:
            inv = inv ** self.GAMMA                 # boost dark tones
        img8 = (inv * 255).astype(np.uint8)
        return cv2.cvtColor(img8, cv2.COLOR_GRAY2BGR)

    # --------- main read ---------
    def read(self, *, as_numpy=True, include_ir=False):
        """
        ret, (colour, depth_vis[, irL, irR, imu_data]) if as_numpy
        ret, (colour_f, depth_f[, irL_f, irR_f, imu_data]) otherwise
        """
        if self.pipeline is None:
            raise RuntimeError("start() must be called first")

        fs = self.pipeline.wait_for_frames()

        # fetch IMU frames
        accel_f = fs.first_or_default(rs.stream.accel)
        gyro_f  = fs.first_or_default(rs.stream.gyro)

        imu_data = {}
        if accel_f:
            m = accel_f.as_motion_frame().get_motion_data()
            imu_data['accel'] = (m.x, m.y, m.z, accel_f.get_timestamp())
        if gyro_f:
            m = gyro_f.as_motion_frame().get_motion_data()
            imu_data['gyro']  = (m.x, m.y, m.z, gyro_f.get_timestamp())

        include_ir = bool(include_ir and self.stream_ir)

        color_f  = fs.get_color_frame()
        depth_f0 = fs.get_depth_frame()
        ir_l_f = ir_r_f = None
        if include_ir:
            ir_l_f = fs.get_infrared_frame(1)
            ir_r_f = fs.get_infrared_frame(2)

        # if any core frame missing, return False + appropriate-length None tuple
        if not color_f or not depth_f0 or (include_ir and (not ir_l_f or not ir_r_f)):
            length = (4 if include_ir else 2) + 1
            return False, (None,) * length

        depth_f = self._filter_depth(depth_f0)

        if not as_numpy:
            base = (color_f, depth_f)
            if include_ir:
                base = base + (ir_l_f, ir_r_f)
            return True, base + (imu_data,)

        colour    = self._np(color_f)
        depth_vis = self._depth_vis(depth_f)

        if not include_ir:
            return True, (colour, depth_vis, imu_data)

        return True, (
            colour,
            depth_vis,
            self._np(ir_l_f),
            self._np(ir_r_f),
            imu_data
        )
