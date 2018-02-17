#!/usr/bin/env python2
'''
    Client code for collecting data with a robot in an episodic manner.
    Triggers model learning when new data is available, and policy learning
    whe new model is available
'''
import argparse
import numpy as np
import os
import rospy
import threading
import yaml
import requests
import pickle

from collections import OrderedDict
from functools import partial
from Queue import Queue, Empty
from ros_plant import ROSPlant

from kusanagi.base import (apply_controller, train_dynamics,
                           preprocess_angles, ExperienceDataset)
from kusanagi.ghost.algorithms import mc_pilco
from kusanagi.ghost.control import RandPolicy
from kusanagi import utils


def numpy_code_constructor(loader, node):
    code_string = loader.construct_scalar(node)
    return eval(code_string)


def include_constructor(loader, node):
    filename = loader.construct_scalar(node)
    if not os.path.isabs(filename):
        root = os.path.dirname(loader.stream.name)
        filename = os.path.abspath(os.path.join(root, filename))
    data = {}
    with open(filename, 'r') as f:
        data = yaml.load(f)
    return data


def default_config():
    config = dict(
        initial_random_trials=4,
        output_directory='/data/robot_learning',
    )
    return config


def parse_config(config_path):
    '''
        loads configuration for learning tasks (policy and costs parameters)
        from a yaml file
    '''
    yaml.add_constructor('!include', include_constructor)
    yaml.add_constructor('!numpy', numpy_code_constructor)
    config = default_config()
    with open(config_path, 'r') as f:
        config = yaml.load(f)
    return config


def mc_pilco_polopt(task_name, task_spec, task_queue):
    '''
    executes one iteration of mc_pilco (model updating and policy optimization)
    '''
    # get task specific variables
    dyn = task_spec['transition_model']
    exp = task_spec['experience']
    pol = task_spec['policy']
    plant_params = task_spec['plant']
    immediate_cost = task_spec['cost']['graph']
    H = int(np.ceil(task_spec['horizon_secs']/plant_params['dt']))
    n_samples = task_spec.get('n_samples', 100)

    if state != 'init':
        # train dynamics model. TODO block if training multiple tasks with
        # the same model
        train_dynamics(
            dyn, exp, pol.angle_dims, wrap_angles=task_spec['wrap_angles'])

        # init policy optimizer if needed
        optimizer = task_spec['optimizer']
        if optimizer.loss_fn is None:
            task_state[task_name] = 'compile_polopt'

            # get policy optimizer options
            split_H = task_spec.get('split_H', 1)
            noisy_policy_input = task_spec.get('noisy_policy_input', False)
            noisy_cost_input = task_spec.get('noisy_cost_input', False)
            truncate_gradient = task_spec.get('truncate_gradient', -1)
            learning_rate = task_spec.get('learning_rate', 1e-3)
            gradient_clip = task_spec.get('gradient_clip', 1.0)

            # get extra inputs, if needed
            import theano.tensor as tt
            ex_in = OrderedDict(
                [(k, v) for k, v in immediate_cost.keywords.items()
                 if type(v) is tt.TensorVariable
                 and len(v.get_parents()) == 0])
            task_spec['extra_in'] = ex_in

            # build loss function
            loss, inps, updts = mc_pilco.get_loss(
                pol, dyn, immediate_cost,
                n_samples=n_samples,
                noisy_cost_input=noisy_cost_input,
                noisy_policy_input=noisy_policy_input,
                split_H=split_H,
                truncate_gradient=(H/split_H)-truncate_gradient,
                crn=100,
                **ex_in)
            inps += ex_in.values()

            # add loss function as objective for optimizer
            optimizer.set_objective(
                loss, pol.get_params(symbolic=True), inps, updts,
                clip=gradient_clip, learning_rate=learning_rate)

        # train policy # TODO block if learning a multitask policy
        task_state[task_name] = 'update_polopt'
        # build inputs to optimizer
        p0 = plant_params['state0_dist']
        gamma = task_spec['discount']
        polopt_args = [p0.mean, p0.cov, H, gamma]
        extra_in = task_spec.get('extra_in', OrderedDict)
        if len(extra_in) > 0:
            polopt_args += [task_spec['cost']['params'][k] for k in extra_in]

        # update dyn and pol (resampling)
        def callback(*args, **kwargs):
            if hasattr(dyn, 'update'):
                dyn.update(n_samples)
            if hasattr(pol, 'update'):
                pol.update(n_samples)
        # call minimize
        callback()
        optimizer.minimize(
            *polopt_args, return_best=task_spec['return_best'])
        task_state[task_name] = 'ready'

    # check if task is done
    n_polopt_iters = len([p for p in exp.policy_parameters if len(p) > 0])
    if n_polopt_iters >= task_spec['n_opt']:
        task_state[task_name] = 'done'
    # put task in the queue for execution
    task_queue.put((task_name, task_spec))

    return


def http_polopt(task_name, task_spec, task_queue):
    # TODO: Automate the harcoded url requests and responses
    url = "http://mc_pilco_server:8008/get_task_init_status/%s" % task_name

    # check if task id exists in server
    http_response = requests.get(url)
    rospy.loginfo(http_response.text)

    # if task_name doesn't exists then upload the task_spec for task_name
    if http_response.text == "get_task_init_status/%s: NOT FOUND" % task_name:
        url = "http://mc_pilco_server:8008/init_task/%s" % task_name
        tspec_pkl = pickle.dumps(task_spec, 2)
        http_response = requests.post(
            url, files={'tspec_file': ('task_spec.pkl', tspec_pkl)})
        rospy.loginfo(http_response.text)
        # TODO: Error Handling

    # TODO: Error handling if the task_spec upload fails
    # send latest experience for task_name
    url = "http://mc_pilco_server:8008/optimize/%s" % task_name
    exp_pkl = pickle.dumps(task_spec['experience'], 2)
    pol_params_pkl = pickle.dumps(
        task_spec['policy'].get_params(symbolic=False), 2)

    http_response = requests.post(
        url,
        files={
            'exp_file': ('experiance.pkl', exp_pkl),
            'pol_params_file': ('policy_params.pkl', pol_params_pkl)
        }
    )

    pol_params = pickle.loads(http_response.text)
    task_spec['policy'].set_params(pol_params)

    task_queue.put((task_name, task_spec))


if __name__ == '__main__':
    np.set_printoptions(linewidth=200, precision=3)
    rospy.init_node('kusanagi_ros', disable_signals=True)

    parser = argparse.ArgumentParser(
        'rosrun robot_learning task_client.py')
    parser.add_argument(
        'config_path', metavar='FILE',
        help='A YAML file containing the configuration for the learning task.',
        type=str)
    parser.add_argument(
        '-p', '--playback', help='Whether to run learnedpolicies only',
        action='store_true')
    parser.add_argument(
        '-t', '--tasks',
        help='Tasks to be executed from the config file. Default is all.',
        type=str, nargs='+', default=[])
    parser.add_argument(
        '-e', '--load_experience',
        help="load past experience if available",
        action="store_true")
    # args = parser.parse_args()
    args = parser.parse_args(rospy.myargv()[1:])
    load_experience = args.load_experience

    # import yaml
    config = parse_config(args.config_path)

    # init output dir
    output_directory = config['output_directory']
    utils.set_output_dir(output_directory)
    try:
        os.mkdir(output_directory)
    except Exception as e:
        if not load_experience:
            # move the old stuff
            dir_time = str(os.stat(output_directory).st_ctime)
            target_dir = os.path.dirname(output_directory)+'_'+dir_time
            os.rename(output_directory, target_dir)
            os.mkdir(output_directory)
            utils.print_with_stamp(
                'Moved old results from [%s] to [%s]' % (output_directory,
                                                         target_dir))

    utils.print_with_stamp('Results will be saved in [%s]' % output_directory)

    # init environment with first task params
    plant_params = config['tasks'].values()[0]['plant']
    env = ROSPlant(**plant_params)

    # init task queue and list of learning threads
    tasks = Queue()
    task_state = {}
    polopt_threads = []

    # populate task queue
    for task_name in config['tasks']:
        spec = config['tasks'][task_name]
        exp = spec.get('experience', None)
        pol = spec['policy']
        if exp is None:
            exp = ExperienceDataset(name=task_name)
            try:
                exp.load()
                if len(exp.policy_parameters) > 0:
                    pol.set_params(exp.policy_parameters[-1])
            except Exception as e:
                pass
        spec['experience'] = exp
        task_state[task_name] = 'init'
        # trigger policy init (for kusanagi only)
        pol.evaluate(np.zeros(pol.D))

        # Optimize policy on the loaded experience
        if len(exp.policy_parameters) > 0:
            http_polopt(task_name, spec, tasks)
        else:
            tasks.put((task_name, spec))

    # while tasks are not done
    while not all([st == 'done' for st in task_state]):
        # get new task
        new_task_ready = False
        rospy.loginfo('Waiting for new task')
        while not new_task_ready:
            try:
                name, spec = tasks.get(timeout=5)
                new_task_ready = True
            except Empty:
                pass
        utils.set_logfile("%s.log" % task_id, base_path="/tmp")
        # if task is done, pass
        state = task_state[name]
        exp = spec.get('experience')
        if state == 'done':
            rospy.loginfo(
                'Finished %s task [iteration %d]' % (name, exp.n_episodes()))
            continue
        rospy.loginfo(
            '==== Executing %s task [iteration %d] ====' % (name,
                                                            exp.n_episodes()))

        # set plant parameters for current task
        plant_params = spec['plant']
        env.init_params(**plant_params)

        # load policy
        if state == 'init' and spec['initial_random_trials'] > 0:
            # collect random experience
            pol = RandPolicy(maxU=spec['policy'].maxU,
                             random_walk=spec.get('random_walk', False))
            polopt_fn = mc_pilco_polopt
            spec['initial_random_trials'] -= 1
            if spec['initial_random_trials'] < 1:
                state = 'ready'
        else:
            # TODO load policy parameters from disk
            pol = spec['policy']
            polopt_fn = spec.get('polopt_fn',
                                 config.get('default_polopt_fn',
                                            http_polopt))

        # set task horizon
        H = int(np.ceil(spec['horizon_secs']/env.dt))

        # execute tasks and collect experience data
        preprocess = None
        if hasattr(pol, 'angle_dims'):
            preprocess = partial(
                preprocess_angles, angle_dims=pol.angle_dims)
        experience = apply_controller(env, pol, H, preprocess)
        # print experience[0]
        # append new experience to dataset
        states, actions, costs, infos = experience
        ts = [info.get('t', None) for info in infos]
        pol_params = (pol.get_params(symbolic=False)
                      if hasattr(pol, 'params') else [])
        exp.append_episode(
            states, actions, costs, infos, pol_params, ts)

        exp.save()
        spec['experience'] = exp

        # launch learning in a separate thread
        new_thread = threading.Thread(name=name, target=polopt_fn,
                                      args=(name, spec, tasks))
        polopt_threads.append(new_thread)
        new_thread.start()
        # polopt_fn(name, spec, tasks)
        # http_polopt(name, spec, tasks)
