#!/usr/bin/env python
import gym
from gym import spaces
from gym.vector import utils
import jax
from jax import numpy as jp
import mujoco
import mujoco.viewer
from mujoco import mjx
import torch
from torch.utils import dlpack as torch_dlpack
from collections import abc
import functools
from typing import Any, Dict, Union
import warnings
import jax
from jax import dlpack as jax_dlpack
from .utils.engine_utils import *
from .utils.mjx_device import device_put
import numpy as np



# Distinct colors for different types of objects.
# For now this is mostly used for visualization.
# This also affects the vision observation, so if training from pixels.
COLOR_BOX = np.array([1, 1, 0, 1])
COLOR_BUTTON = np.array([1, .5, 0, 1])
COLOR_GOAL = np.array([0, 1, 0, 1])
COLOR_VASE = np.array([0, 1, 1, 1])
COLOR_HAZARD = np.array([0, 0, 1, 1])
COLOR_HAZARD3D = np.array([0, 0, 1, 1])
COLOR_PILLAR = np.array([.5, .5, 1, 1])
COLOR_WALL = np.array([.5, .5, .5, 1])
COLOR_GREMLIN = np.array([0.5, 0, 1, 1])
COLOR_CIRCLE = np.array([0, 1, 0, 1])
COLOR_RED = np.array([1, 0, 0, 1])
COLOR_GHOST = np.array([1, 0, 0.5, 1])
COLOR_GHOST3D = np.array([1, 0, 0.5, 1])
COLOR_ROBBER = np.array([1, 1, 0.5, 1])
COLOR_ROBBER3D = np.array([1, 1, 0.5, 1])

# Groups are a mujoco-specific mechanism for selecting which geom objects to "see"
# We use these for raycasting lidar, where there are different lidar types.
# These work by turning "on" the group to see and "off" all the other groups.
# See obs_lidar_natural() for more.
GROUP_GOAL = 0
GROUP_BOX = 1
GROUP_BUTTON = 1
GROUP_WALL = 2
GROUP_PILLAR = 2
GROUP_HAZARD = 3
GROUP_VASE = 4
GROUP_GREMLIN = 5
GROUP_CIRCLE = 6
GROUP_HAZARD3D = 3
GROUP_GHOST = 3
GROUP_GHOST3D = 3
GROUP_ROBBER = 5
GROUP_ROBBER3D = 5



# Constant for origin of world
ORIGIN_COORDINATES = np.zeros(3)

# Constant defaults for rendering frames for humans (not used for vision)
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080

class ResamplingError(AssertionError):
    ''' Raised when we fail to sample a valid distribution of objects or goals '''
    pass


class Engine(gym.Env, gym.utils.EzPickle):

    '''
    Engine: an environment-building tool for safe exploration research.

    The Engine() class entails everything to do with the tasks and safety 
    requirements of Safety Gym environments. An Engine() uses a World() object
    to interface to MuJoCo. World() configurations are inferred from Engine()
    configurations, so an environment in Safety Gym can be completely specified
    by the config dict of the Engine() object.

    '''


    def __init__(self, config={}):
        # First, parse configuration. Important note: LOTS of stuff happens in
        # parse, and many attributes of the class get set through setattr. If you
        # are trying to track down where an attribute gets initially set, and 
        # can't find it anywhere else, it's probably set via the config dict
        # and this parse function.
        self.body_name2id = {}
        self.hazards_size = 0.3
        self.goal_size = 0.5
        self.device_id = 0
        self._seed(10) # generate self._key for jax randomization
        
        self._physics_steps_per_control_step = 1 # number of steps per Mujoco step
        
        path = './safe_rl_envs/xmls/point.xml'
        
        self.mj_model = mujoco.MjModel.from_xml_path(path) # Load Mujoco model from xml
        self.mj_data = mujoco.MjData(self.mj_model) # Genearte Mujoco data from Mujoco model
        self.mjx_model = device_put(self.mj_model, self.device_id) # Convert Mujoco model to MJX model for device acceleration
        self.mjx_data = device_put(self.mj_data, self.device_id) # Convert Mujoco data to MJX data for device acceleration
        
        
        
        self.num_envs = 1 # Number of the batched environment
        self.key = jax.random.PRNGKey(0) # Number of the batched environment

        # Use jax.vmap to warp single environment reset to batched reset function
        def batched_reset(rng: jax.Array):
            if self.num_envs is not None:
                rng = jax.random.split(rng, self.num_envs)
            return jax.vmap(self.mjx_reset)(rng)
        
        # Use jax.vmap to warp single environment step to batched step function
        def batched_step(data: mjx.Data, action: jax.Array, rng: jax.Array):
            if self.num_envs is not None:
                rng = jax.random.split(rng, self.num_envs)
            return jax.vmap(self.mjx_step)(data, action, rng)
        
        self._reset = jax.jit(batched_reset) # Use Just In Time (JIT) compilation to execute batched reset efficiently
        self._step = jax.jit(batched_step) # Use Just In Time (JIT) compilation to execute batched step efficiently
        self.data = None # Keep track of the current state of the whole batched environment with self.data
        self.viewer = None
        self.hazards_placements = [-2,2]
        
        ctrl_range = self.mj_model.actuator_ctrlrange
        ctrl_range[~(self.mj_model.actuator_ctrllimited == 1), :] = np.array([-np.inf, np.inf])
        action = jax.tree_map(np.array, ctrl_range)
        action_space = spaces.Box(action[:, 0], action[:, 1], dtype='float32')
        self.action_space = utils.batch_space(action_space, self.num_envs)
        
    # @property
    # def model(self):
    #     ''' Helper to get the world's model instance '''
    #     return self.sim.model

    # @property
    # def data(self):
    #     ''' Helper to get the world's simulation data instance '''
    #     return self.sim.data

    # @property
    # def robot_pos(self):
    #     ''' Helper to get current robot position '''
    #     return self.data.get_body_xpos('robot').copy()

    # @property
    # def goal_pos(self):
    #     ''' Helper to get goal position from layout '''
    #     if self.task in ['goal', 'push']:
    #         return self.data.get_body_xpos('goal').copy()
    #     elif self.task == 'button':
    #         return self.data.get_body_xpos(f'button{self.goal_button}').copy()
    #     elif self.task == 'circle':
    #         return ORIGIN_COORDINATES
    #     elif self.task == 'none':
    #         return np.zeros(2)  # Only used for screenshots
    #     elif self.task == 'chase':
    #         return ORIGIN_COORDINATES
    #     elif self.task == 'defense':
    #         return ORIGIN_COORDINATES
    #     else:
    #         raise ValueError(f'Invalid task {self.task}')

    # @property
    # def hazards_pos(self):
    #     ''' Helper to get the hazards positions from layout '''
    #     return [self.data.get_body_xpos(f'hazard{i}').copy() for i in range(self.hazards_num)]

    def _seed(self, seed: int = 0):
        self.key = jax.random.PRNGKey(seed)

    def mjx_reset(self, rng):
        """ Resets an unbatched environment to an initial state."""
        layout = self.build_layout(rng)
        data = self.mjx_data
        xpos = self.mjx_data.xpos
        xpos = xpos.at[3:].set(layout["hazards_pos"])
        print(xpos.shape)
        # data = data.replace(xpos=jp.zeros(self.mjx_model.nbody), qpos=jp.zeros(self.mjx_model.nq), qvel=jp.zeros(self.mjx_model.nv), ctrl=jp.zeros(self.mjx_model.nu))
        data = mjx.forward(self.mjx_model, data)
        data = mjx.step(self.mjx_model, data)
        obs = self._get_obs(data)
        
        return obs, data

    def mjx_step(self, data: mjx.Data, action: jp.ndarray, rng):
        """ Runs one timestep of an unbatched environment's dynamics."""
        
        def f(data, _):
            # data = data.replace(ctrl=action)
            qpos = jax.random.uniform(rng, (self.mjx_model.nq,), minval=-1, maxval=1)
            data = data.replace(qpos=qpos, qvel=jp.zeros(self.mjx_model.nv), ctrl=jp.zeros(self.mjx_model.nu))
            return (
                mjx.step(self.mjx_model, data),
                None,
            )
        data, _ = jax.lax.scan(f, data, (), self._physics_steps_per_control_step)
        obs = self._get_obs(data)
        return obs, data
    
    def build_observation_space(self):
        pass

    def reset(self):
        ''' Reset the physics simulation and return observation '''
        obs, self.data= self._reset(self.key)
        return jax_to_torch(obs)

    
    def step(self, action):
        ''' Take a step and return observation, reward, done, and info '''
        action = torch_to_jax(action)
        self.key,_ = jax.random.split(self.key, 2)
        obs, self.data= self._step(self.data, action, self.key)
        print(self.data.xpos.shape)
        return jax_to_torch(obs)
    
    def build_layout(self, rng):
        ''' Rejection sample a placement of objects to find a layout. '''
        # if not self.randomize_layout:
        #     self.rs = np.random.RandomState(0)
        rng, rng1, rng2 = jax.random.split(rng, 3)
        
        xpos = jax.random.uniform(rng1, (8,3), minval=self.hazards_placements[0], maxval=self.hazards_placements[1])
        layout = {}
        layout["hazards_pos"] = xpos
        return layout
        
    def _get_obs(self, data: mjx.Data) -> jp.ndarray:
        #TODO: Weiye
        obs = data.xpos
        robot_pos = data.xpos[1,:]
        robot_mat = data.xmat[1,:,:]
        hazard_pos = data.xpos[3:,:]
        def ego_xy(pos):
            ''' Return the egocentric XY vector to a position from the robot '''
            assert pos.shape == (2,), f'Bad pos {pos}'
            pos_3vec = jp.concatenate([pos, jp.array([0])]) # Add a zero z-coordinate
            world_3vec = pos_3vec - robot_pos
            return jp.matmul(world_3vec, robot_mat)[:2] # only take XY coordinates

        def torch_angle(real_part, imag_part):
            '''Returns angle in radian, shape = B, where B is batch size'''
            # Calculate the angle (phase) in radians
            angle_rad = jp.arctan2(imag_part, real_part)
            return angle_rad

        def obs_lidar_pseudo(positions, lidar_num_bins = 16, lidar_max_dist=None, lidar_exp_gain = 1.0,
                            lidar_alias = True):
            '''
            Return a robot-centric lidar observation of a list of positions.


            Lidar is a set of bins around the robot (divided evenly in a circle).
            The detection directions are exclusive and exhaustive for a full 360 view.
            Each bin reads 0 if there are no objects in that direction.
            If there are multiple objects, the distance to the closest one is used.
            Otherwise the bin reads the fraction of the distance towards the robot.


            E.g. if the object is 90% of lidar_max_dist away, the bin will read 0.1,
            and if the object is 10% of lidar_max_dist away, the bin will read 0.9.
            (The reading can be thought of as "closeness" or inverse distance)


            This encoding has some desirable properties:
            - bins read 0 when empty
            - bins smoothly increase as objects get close
            - maximum reading is 1.0 (where the object overlaps the robot)
            - close objects occlude far objects
            - constant size observation with variable numbers of objects
            '''
            
            obs = jp.zeros(lidar_num_bins)
            for pos in positions:
                pos = jp.asarray(pos)
                if pos.shape == (3,):
                    pos = pos[:2] # Truncate Z coordinate
                z = ego_xy(pos)
                dist = jp.linalg.norm(z) # shape = B
                angle = torch_angle(real_part=z[0], imag_part=z[1]) % (jp.pi * 2) # shape = B
                # z = complex(*ego_xy(pos)) # X, Y as real, imaginary components
                # dist = jp.abs(z)
                # angle = jp.angle(z) % (jp.pi * 2)
                bin_size = (jp.pi * 2) / lidar_num_bins
                
                bin = (angle / bin_size).astype(int)
                bin_angle = bin_size * bin
                # import ipdb; ipdb.set_trace()
                if lidar_max_dist is None:
                    sensor = jp.exp(-lidar_exp_gain * dist)
                else:
                    sensor = max(0, lidar_max_dist - dist) / lidar_max_dist
                senor_new = jp.maximum(obs[bin], sensor)
                obs = obs.at[bin].set(senor_new)
                # Aliasing
                # this is to make the neighborhood bins has sense of the what's happending in the bin that has obstacle
                if lidar_alias:
                    alias_jp = (angle - bin_angle) / bin_size
                    # assert 0 <= alias_jp <= 1, f'bad alias_jp {alias_jp}, dist {dist}, angle {angle}, bin {bin}'
                    bin_plus = (bin + 1) % lidar_num_bins
                    bin_minus = (bin - 1) % lidar_num_bins
                    obs = obs.at[bin_plus].set(jp.maximum(obs[bin_plus], alias_jp * sensor))
                    obs = obs.at[bin_minus].set(jp.maximum(obs[bin_minus], (1 - alias_jp) * sensor))
            return obs

        obs = obs_lidar_pseudo(hazard_pos)
        # import ipdb;ipdb.set_trace()
        return obs

    def mjx_step_newer(self, data: mjx.Data, 
                 last_data: mjx.Data, 
                 last_last_data: mjx.Data, 
                 last_dist_goal: mjx.Data,
                 action: jp.ndarray):
        #! TODO: be able to send the last data and last last data and last dist goal 
        """Runs one timestep of the environment's dynamics."""
        def f(data, _):
            data = data.replace(ctrl=action)
            return (
                mjx.step(self.mjx_model, data),
                None,
            )
        data, _ = jax.lax.scan(f, data, (), self._physics_steps_per_control_step)
        obs = self._get_obs_newer(data, last_data, last_last_data)
        reward, done = self._get_reward_done(data, last_dist_goal)
        cost = self._get_cost(data)
        info['cost'] = cost
        
        return obs, reward, done, info, data
    
    def _get_obs_newer(self, data: mjx.Data, last_data: mjx.Data, last_last_data: mjx.Data) -> jp.ndarray:
        # get the raw position data of different objects current frame 
        robot_pos = data.xpos[self.body_name2id['robot'],:]
        robot_mat = data.xmat[self.body_name2id['robot'],:,:]
        hazard_pos = data.xpos[self.body_name2id['hazards'],:]
        goal_pos = data.xpos[self.body_name2id['goal'],:]
        
        # get the raw joint info current frame 
        #! TODO: check jpos and following correctness
        jpos = data.jpos[self.body_name2id['robot'],:]
        jvel = data.jvel[self.body_name2id['robot'],:]
        jacc = data.jacc[self.body_name2id['robot'],:]
        
        # get the raw position data of previous frames
        robot_pos_last = last_data.xpos[self.body_name2id['robot'],:]
        robot_pos_last_last = last_last_data.xpos[self.body_name2id['robot'],:]
        
        def ego_xy(pos):
            ''' Return the egocentric XY vector to a position from the robot '''
            assert pos.shape == (2,), f'Bad pos {pos}'
            pos_3vec = jp.concatenate([pos, jp.array([0])]) # Add a zero z-coordinate
            world_3vec = pos_3vec - robot_pos
            return jp.matmul(world_3vec, robot_mat)[:2] # only take XY coordinates

        def jax_angle(real_part, imag_part):
            '''Returns robot heading angle in the world frame'''
            # Calculate the angle (phase) in radians
            angle_rad = jp.arctan2(imag_part, real_part)
            return angle_rad

        def obs_lidar_pseudo(positions):
            '''
            Return a robot-centric lidar observation of a list of positions.


            Lidar is a set of bins around the robot (divided evenly in a circle).
            The detection directions are exclusive and exhaustive for a full 360 view.
            Each bin reads 0 if there are no objects in that direction.
            If there are multiple objects, the distance to the closest one is used.
            Otherwise the bin reads the fraction of the distance towards the robot.


            E.g. if the object is 90% of lidar_max_dist away, the bin will read 0.1,
            and if the object is 10% of lidar_max_dist away, the bin will read 0.9.
            (The reading can be thought of as "closeness" or inverse distance)


            This encoding has some desirable properties:
            - bins read 0 when empty
            - bins smoothly increase as objects get close
            - maximum reading is 1.0 (where the object overlaps the robot)
            - close objects occlude far objects
            - constant size observation with variable numbers of objects
            '''
            
            obs = jp.zeros(self.lidar_num_bins)
            for pos in positions:
                pos = jp.asarray(pos)
                if pos.shape == (3,):
                    pos = pos[:2] # Truncate Z coordinate
                z = ego_xy(pos) # (X, Y) to be treated as real, imaginary components
                dist = jp.linalg.norm(z) # shape = B
                angle = jax_angle(real_part=z[0], imag_part=z[1]) % (jp.pi * 2) # shape = B
                bin_size = (jp.pi * 2) / self.lidar_num_bins
                
                bin = (angle / bin_size).astype(int)
                bin_angle = bin_size * bin
                # import ipdb; ipdb.set_trace()
                if self.lidar_max_dist is None:
                    sensor = jp.exp(-self.lidar_exp_gain * dist)
                else:
                    sensor = max(0, self.lidar_max_dist - dist) / self.lidar_max_dist
                senor_new = jp.maximum(obs[bin], sensor)
                obs = obs.at[bin].set(senor_new)
                # Aliasing
                # this is to make the neighborhood bins has sense of the what's happending in the bin that has obstacle
                if self.lidar_alias:
                    alias_jp = (angle - bin_angle) / bin_size
                    assert 0 <= alias_jp <= 1, f'bad alias_jp {alias_jp}, dist {dist}, angle {angle}, bin {bin}'
                    bin_plus = (bin + 1) % self.lidar_num_bins
                    bin_minus = (bin - 1) % self.lidar_num_bins
                    obs = obs.at[bin_plus].set(jp.maximum(obs[bin_plus], alias_jp * sensor))
                    obs = obs.at[bin_minus].set(jp.maximum(obs[bin_minus], (1 - alias_jp) * sensor))
            return obs
        
        def ego_vel_acc():
            '''velocity and acceleration in the robot frame '''
            # current velocity
            pos_diff_vec_world_frame = robot_pos - robot_pos_last
            vel_vec_world_frame = pos_diff_vec_world_frame / self.timestep
            
            # last velocity 
            last_pos_diff_vec_world_frame = robot_pos_last - robot_pos_last_last
            last_vel_vec_world_frame = last_pos_diff_vec_world_frame / self.timestep
            
            # current acceleration
            acc_vec_world_frame = (vel_vec_world_frame - last_vel_vec_world_frame) / self.timestep
            
            # to robot frame 
            vel_vec_robot_frame = jp.matmul(vel_vec_world_frame, robot_mat)[:2] # only take XY coordinates
            acc_vec_robot_frame = jp.matmul(acc_vec_world_frame, robot_mat)[:2] # only take XY coordinates
            
            return vel_vec_robot_frame, acc_vec_robot_frame

        hazards_lidar = obs_lidar_pseudo(hazard_pos)
        goal_lidar = obs_lidar_pseudo(goal_pos)
        vel_vec, acc_vec = ego_vel_acc()

        # concatenate processed data for different objects together 
        obs = jp.concatenate([jpos, jvel, jacc, vel_vec, acc_vec, hazards_lidar, goal_lidar])
        return obs

    def _get_reward_done(self, data: mjx.Data, last_dist_goal: mjx.Data) -> Tuple[jp.ndarray, jp.bool]:        
        # get raw data of robot pos and goal pos
        robot_pos = data.xpos[self.body_name2idx['robot'],:]
        goal_pos = data.xpos[self.body_name2id['goal'],:]
        assert robot_pos.shape == (3,) and goal_pos.shape == (3,)
        # Return the distance from the robot to an XY position
        dist_goal = jp.sqrt(jp.sum(jp.square(goal_pos[:2] - robot_pos[:2]))) 
        
        reward += (last_dist_goal - dist_goal) * self.reward_distance
        if dist_goal < self.goal_size:
            done = jp.array(True)
        else:
            done = jp.array(False)
        #! TODO: update the last dist goal 
        return reward, done
    
    def _get_cost(self, data: mjx.Data) -> jp.ndarray:
        # get the raw position data of different objects current frame 
        robot_pos = data.xpos[self.body_name2id['robot'],:]
        hazard_pos = data.xpos[self.body_name2id['hazards'],:]
        dist_robot2hazard = jp.linalg.norm(hazard_pos[:,:2] - robot_pos[:,:2], axis=1)
        dist_robot2hazard_below_threshold = jp.maximum(dist_robot2hazard, self.hazards_size)
        cost = jp.sum(self.hazards_size*jp.ones(dist_robot2hazard_below_threshold.shape) - dist_robot2hazard_below_threshold)
        return cost 
       

    def render_lidar(self, poses, color, offset, group):
        pass

    def render_sphere(self, pos, size, color, label='', alpha=0.1):
        pass

    def viewer_setup(self):
        # self.viewer.cam.trackbodyid = 0         # id of the body to track ()
        # self.viewer.cam.distance = self.model.stat.extent * 3       # how much you "zoom in", model.stat.extent is the max limits of the arena
        self.viewer.cam.distance = 10
        self.viewer.cam.lookat[0] = 0         # x,y,z offset from the object (works if trackbodyid=-1)
        self.viewer.cam.lookat[1] = -3
        self.viewer.cam.lookat[2] = 5
        self.viewer.cam.elevation = -60           # camera rotation around the axis in the plane going through the frame origin (if 0 you just see a line)
        self.viewer.cam.azimuth = 90              # camera rotation around the camera's vertical axis
        self.viewer.opt.geomgroup = 1    

    def render(self):
        mjx.device_get_into(self.mj_data, self.data)
        if self.viewer is None:
            self.viewer = mujoco.viewer.launch_passive(self.mj_model, self.mj_data)
            self.viewer_setup()
        mujoco.mj_step(self.mj_model, self.mj_data)
        self.viewer.sync()