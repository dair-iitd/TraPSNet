import better_exceptions

import unittest
import sys
import os
import numpy as np
import tensorflow as tf
import itertools
import shutil
import threading
import multiprocessing

curr_dir_path = os.path.dirname(os.path.realpath(__file__))
gym_path = os.path.abspath(os.path.join(curr_dir_path, "../.."))
if gym_path not in sys.path:
    sys.path = [gym_path] + sys.path
parser_path = os.path.abspath(os.path.join(curr_dir_path, "../../utils"))
if parser_path not in sys.path:
    sys.path = [parser_path] + sys.path
import gym

from estimators import ValueEstimator, PolicyEstimator
from policy_monitor import PolicyMonitor
from parse_instance import InstanceParser
from worker import Worker

# os.environ['CUDA_VISIBLE_DEVICES'] = ''

tf.flags.DEFINE_string("model_dir", "./train",
                       "Directory to write Tensorboard summaries to.")
tf.flags.DEFINE_string("domain", None, "Name of domain")
tf.flags.DEFINE_string("instance", None, "Name of instance")
tf.flags.DEFINE_integer("num_instances", None, "Name of number of instances")
tf.flags.DEFINE_integer("num_features", 3, "Number of features in graph CNN")
tf.flags.DEFINE_integer("num_policy_rl", 20, "Number of features in graph CNN")
tf.flags.DEFINE_integer("num_value_rl", 20, "Number of features in graph CNN")
tf.flags.DEFINE_integer("num_attention_dim", 20,
                        "Number of attention dimensions in the network")
tf.flags.DEFINE_integer("num_action_embed", 20,
                        "Number of action dimensions in the network")
tf.flags.DEFINE_integer("num_transition_dim", 20,
                        "Number of transition dimensions in the network")
tf.flags.DEFINE_string("activation", "elu", "Activation function")
tf.flags.DEFINE_string("gpuid", None, "Activation function")
tf.flags.DEFINE_float("lr", 5e-5, "Learning rate")
tf.flags.DEFINE_integer("t_max", 20,
                        "Number of steps before performing an update")
tf.flags.DEFINE_integer(
    "max_global_steps", None,
    "Stop training after this many steps in the environment. Defaults to running indefinitely."
)
tf.flags.DEFINE_boolean("lr_decay", False, "If set, decay learning rate")
tf.flags.DEFINE_boolean(
    "use_pretrained", False,
    "If set, load weights from pretrained model (if found in checkpoint dir).")
tf.flags.DEFINE_integer("eval_every", 300,
                        "Evaluate the policy every N seconds")
tf.flags.DEFINE_boolean(
    "reset", False,
    "If set, delete the existing model directory and start training from scratch."
)
tf.flags.DEFINE_integer(
    "parallelism", None,
    "Number of threads to run. If not set we run [num_cpu_cores] threads.")

FLAGS = tf.flags.FLAGS

instances = []
for instance_num in FLAGS.instance.strip().split(","):
    if FLAGS.num_instances > 0:
        for i in range(FLAGS.num_instances):
            instance = "{}.{}".format(instance_num, (i + 1))
            instances.append(instance)
    else:
        instance = "{}".format(instance_num)
        instances.append(instance)

FLAGS.num_instances = len(instances)


def make_envs():
    envs = []
    for i in range(FLAGS.num_instances):
        env_name = "RDDL-{}{}-v1".format(FLAGS.domain, instances[i])
        env = gym.make(env_name)
        envs.append(env)
    return envs


# action space
print("Finding state and action parameters from env")
envs_ = make_envs()

num_inputs_list = []
num_valid_actions_list = []
for env_ in envs_:
    num_action_vars = env_.num_action_vars
    num_valid_actions = env_.num_action_vars + 2  # TODO: check
    state_dim = env_.num_state_vars
    VALID_ACTIONS = list(range(num_valid_actions))
    num_inputs_list.append(state_dim)
    num_valid_actions_list.append(num_valid_actions)

for e in envs_:
    e.close()

# nn hidden layer parameters
policy_num_gcn_hidden = FLAGS.num_features
policy_num_rl_hidden = FLAGS.num_policy_rl

value_num_gcn_hidden = FLAGS.num_features
value_num_rl_hidden = FLAGS.num_value_rl

num_hidden_transition = FLAGS.num_transition_dim

# Number of input features
instance_parser_ = InstanceParser(FLAGS.domain, '1')
fluent_feature_dims = instance_parser_.fluent_feature_dims
nonfluent_feature_dims = instance_parser_.nonfluent_feature_dims

MODEL_DIR = FLAGS.model_dir
model_suffix = "{}{}-{}-{}-{}-{}-{}-{}-{}-{}".format(
    FLAGS.domain, FLAGS.instance, FLAGS.activation, FLAGS.num_features,
    policy_num_rl_hidden, value_num_rl_hidden, FLAGS.num_attention_dim,
    FLAGS.num_transition_dim, FLAGS.num_action_embed, FLAGS.lr)
MODEL_DIR = os.path.abspath(os.path.join(MODEL_DIR, model_suffix))

if not os.path.exists(MODEL_DIR):
    os.makedirs(MODEL_DIR)
CHECKPOINT_DIR = os.path.join(MODEL_DIR, "checkpoints")

# Set the number of workers
NUM_WORKERS = multiprocessing.cpu_count() - 2
if FLAGS.parallelism:
    NUM_WORKERS = FLAGS.parallelism

# Optionally empty model directory
if FLAGS.reset:
    shutil.rmtree(MODEL_DIR, ignore_errors=True)

if not os.path.exists(CHECKPOINT_DIR):
    os.makedirs(CHECKPOINT_DIR)

train_summary_writer = tf.summary.FileWriter(
    os.path.join(MODEL_DIR, "train_summaries"))
val_summary_writer = tf.summary.FileWriter(
    os.path.join(MODEL_DIR, "val_summaries"))

# Keeps track of the number of updates we've performed
global_step = tf.Variable(0, name="global_step", trainable=False)

if FLAGS.lr_decay:
    global_learning_rate = tf.train.exponential_decay(
        FLAGS.lr, global_step, 500000, 0.3, staircase=True)
else:
    global_learning_rate = FLAGS.lr

# Global policy and value nets
with tf.variable_scope("global") as vs:
    policy_net = PolicyEstimator(
        num_inputs_list=num_inputs_list,
        fluent_feature_dims=fluent_feature_dims,
        nonfluent_feature_dims=nonfluent_feature_dims,
        N=FLAGS.num_instances,
        num_gcn_hidden=policy_num_gcn_hidden,
        num_attention_dim=FLAGS.num_attention_dim,
        num_rl_hidden=policy_num_rl_hidden,
        num_action_embed=FLAGS.num_action_embed,
        num_hidden_transition=num_hidden_transition,
        num_outputs_list=num_valid_actions_list,
        activation=FLAGS.activation,
        learning_rate=global_learning_rate)
    value_net = ValueEstimator(
        num_inputs_list=num_inputs_list,
        fluent_feature_dims=fluent_feature_dims,
        nonfluent_feature_dims=nonfluent_feature_dims,
        N=FLAGS.num_instances,
        num_gcn_hidden=value_num_gcn_hidden,
        num_attention_dim=FLAGS.num_attention_dim,
        num_rl_hidden=value_num_rl_hidden,
        activation=FLAGS.activation,
        learning_rate=global_learning_rate)

# Global step iterator
global_counter = itertools.count()

# Session configs
if FLAGS.gpuid is not None:
    os.environ["CUDA_VISIBLE_DEVICES"] = FLAGS.gpuid
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
# config.gpu_options.per_process_gpu_memory_fraction = 0.95

with tf.device("/cpu:0"):

    # Create worker graphs
    workers = []
    for worker_id in range(NUM_WORKERS):
        # We only write summaries in one of the workers because they're
        # pretty much identical and writing them on all workers
        # would be a waste of space
        worker_summary_writer = None
        if worker_id == 0:
            worker_summary_writer = train_summary_writer

        worker = Worker(
            name="worker_{}".format(worker_id),
            envs=make_envs(),
            policy_net=policy_net,
            value_net=value_net,
            global_counter=global_counter,
            domain=FLAGS.domain,
            instances=instances,
            discount_factor=0.99,
            summary_writer=worker_summary_writer,
            max_global_steps=FLAGS.max_global_steps)
        workers.append(worker)

    saver = tf.train.Saver(keep_checkpoint_every_n_hours=1.0, max_to_keep=1)

    # Used to occasionally write episode rewards to Tensorboard
    pe = PolicyMonitor(
        envs=make_envs(),
        policy_net=policy_net,
        domain=FLAGS.domain,
        instances=instances,
        summary_writer=val_summary_writer,
        saver=saver)

with tf.Session(config=config) as sess:
    sess.run(tf.global_variables_initializer())
    train_summary_writer.add_graph(sess.graph)
    # val_summary_writer.add_graph(sess.graph)
    coord = tf.train.Coordinator()

    if FLAGS.use_pretrained:
        # Load a previous checkpoint if it exists
        ckpt = tf.train.get_checkpoint_state(
            os.path.dirname(CHECKPOINT_DIR + '/checkpoint'))
        if ckpt and ckpt.model_checkpoint_path:
            print(("Loading model checkpoint: {}".format(
                ckpt.model_checkpoint_path)))
            saver.restore(sess, ckpt.model_checkpoint_path)
        else:
            print("Training new model")

    # Start worker threads
    worker_threads = []
    for worker in workers:

        def worker_fn():
            return worker.run(sess, coord, FLAGS.t_max)

        t = threading.Thread(target=worker_fn)
        t.start()
        worker_threads.append(t)

    # Start a thread for policy eval task
    monitor_thread = threading.Thread(
        target=lambda: pe.continuous_eval(FLAGS.eval_every, sess, coord))
    monitor_thread.start()

    # Wait for all workers to finish
    coord.join(worker_threads)