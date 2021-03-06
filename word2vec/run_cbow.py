from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math, os, shutil, sys
import random, time
import logging

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
sys.path.insert(0, '../utils')

from tasks import Task

from prepare_train import positive_items, item_frequency, sample_items, to_week
from data_iterator import DataIterator
# in order to profile
from tensorflow.python.client import timeline

tf.app.flags.DEFINE_float("learning_rate", 0.1, "Learning rate.")
tf.app.flags.DEFINE_float("keep_prob", 0.5, "dropout rate.")
tf.app.flags.DEFINE_float("power", 0.5, "related to sampling rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 1.0,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 64,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 20, "Size of each model layer.")
tf.app.flags.DEFINE_integer("hidden_size", 500, "when nonlinear proj used")
tf.app.flags.DEFINE_integer("n_resample", 50, "iterations before resample.")
tf.app.flags.DEFINE_integer("output_feat", 1, "0: no use, 1: use, mean-mulhot, 2: use, max-pool")
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("user_vocab_size", 150000, "User vocabulary size.")
tf.app.flags.DEFINE_integer("item_vocab_size", 50000, "Item vocabulary size.")
tf.app.flags.DEFINE_integer("n_sampled", 1024, "sampled softmax/warp loss.")
tf.app.flags.DEFINE_integer("ni", 0, "# of items used to test")
tf.app.flags.DEFINE_string("dataset", "xing", ".")
tf.app.flags.DEFINE_string("data_dir", "./data0", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "./test0", "Training directory.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("patience", 20,
                            "exit if the model can't improve for $patience evals")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 4000,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("use_user_feature", True, "RT")
tf.app.flags.DEFINE_boolean("use_item_feature", True, "RT")
tf.app.flags.DEFINE_boolean("use_sep_item", True, "use separate embedding parameters for output items.")
tf.app.flags.DEFINE_boolean("recommend", False,
                            "Set to True for recommend items.")
tf.app.flags.DEFINE_boolean("recommend_new", False,
                            "Set to True for recommend new items that were not used to train.")
tf.app.flags.DEFINE_boolean("device_log", False,
                            "Set to True for logging device usages.")
tf.app.flags.DEFINE_boolean("eval", True,
                            "Set to True for evaluation.")
tf.app.flags.DEFINE_boolean("use_more_train", False,
                            "Set true if use non-appearred items to train.")
tf.app.flags.DEFINE_boolean("profile", False, "False = no profile, True = profile")
tf.app.flags.DEFINE_boolean("after40", False,
                            "whether use items after week 40 only.")
tf.app.flags.DEFINE_string("loss", 'ce',
                            "loss function")
tf.app.flags.DEFINE_string("model_option", 'loss',
                            "model to evaluation")
tf.app.flags.DEFINE_boolean("test", False, "Test on test splits")
tf.app.flags.DEFINE_integer("n_epoch", 1000, "How many epochs to train.")


tf.app.flags.DEFINE_integer("num_skips", 3, "Size of each model layer.")
tf.app.flags.DEFINE_integer("skip_window", 5, "Size of each model layer.")

# Xing related
tf.app.flags.DEFINE_integer("ta", 1, "target_active")
tf.app.flags.DEFINE_float("user_sample", 1.0, "user sample rate.")
tf.app.flags.DEFINE_integer("top_N_items", 100,
                            "number of items output")

FLAGS = tf.app.flags.FLAGS

def mylog(msg):
  print(msg)
  logging.info(msg)
  return

def get_user_items_seq(data):
  # group (u,i) by user and sort by time
  d = {}
  for u, i, t in data:
    if FLAGS.after40 and to_week(t) < 40:
      continue
    if u not in d:
      d[u] = []
    d[u].append((i,t))
  for u in d:
    tmp = sorted(d[u], key=lambda x:x[1])
    tmp = [x[0] for x in tmp]
    assert(len(tmp)>0)
    d[u] = tmp
  return d

def form_train_seq(x, pad_token, opt=1):
  # train corpus
  seq = []
  p = pad_token
  for u in x:
    l = [(u,i) for i in x[u]]
    if opt == 0:
      seq.extend(l)
      seq.append((u, p))
    else:
      seq.append((u, p))
      seq.extend(l)

  return seq

def prepare_valid(data_va, u_i_seq_tr, end_ind, n=0):
  res = {}
  processed = set([])
  for u, _, _ in data_va:
    if u in processed:
      continue
    processed.add(u)

    if u in u_i_seq_tr:
      if n == 0:
        res[u] = []
      elif n == -1 :
        res[u] = [end_ind]
      else:
        items = u_i_seq_tr[u][-n:]
        l = len(items)
        if l < n:
          items += [end_ind] * (n-l)
        res[u] = items
    else:
      if n == -1:
        res[u] = [end_ind]
      else:
        res[u] = [end_ind] * n
  return res

def read_data(task):
  data_read = task.get_dataread()
  
  _submit = 1 if FLAGS.test else 0
  (data_tr0, data_va0, u_attr, i_attr, item_ind2logit_ind, 
    logit_ind2item_ind) = data_read(FLAGS.data_dir, _submit = _submit, ta = FLAGS.ta, 
    logits_size_tr=FLAGS.item_vocab_size, sample=FLAGS.user_sample)
  print('length of item_ind2logit_ind', len(item_ind2logit_ind))

  #remove some rare items in both train and valid set
  #this helps make train/valid set distribution similar 
  #to each other
  
  mylog("original train/dev size: %d/%d" %(len(data_tr0),len(data_va0)))
  data_tr = [p for p in data_tr0 if (p[1] in item_ind2logit_ind)]
  data_va = [p for p in data_va0 if (p[1] in item_ind2logit_ind)]
  mylog("new train/dev size: %d/%d" %(len(data_tr),len(data_va)))

  if not FLAGS.use_item_feature:
    mylog("NOT using item attributes")
    i_attr.num_features_cat = 1
    i_attr.num_features_mulhot = 0
  if not FLAGS.use_user_feature:
    mylog("NOT using user attributes")
    u_attr.num_features_cat = 1
    u_attr.num_features_mulhot = 0

  if FLAGS.dataset == 'ml':
    print('disabling the lstm-rec fake feature')
    u_attr.num_features_cat = 1

  u_i_seq_tr = get_user_items_seq(data_tr)
  PAD_ID = i_attr.get_item_last_index()
  seq_tr = form_train_seq(u_i_seq_tr, PAD_ID)
  items_dev = prepare_valid(data_va0, u_i_seq_tr, PAD_ID, FLAGS.ni)

  return (seq_tr, items_dev, data_tr, data_va, u_attr, i_attr, 
    item_ind2logit_ind, logit_ind2item_ind, PAD_ID)

def create_model(session, u_attributes=None, i_attributes=None, 
  item_ind2logit_ind=None, logit_ind2item_ind=None, 
  loss = FLAGS.loss, logit_size_test=None, ind_item = None):  
  n_sampled = FLAGS.n_sampled if FLAGS.loss in ['mw', 'mce'] else None
  import cbow_model
  model = cbow_model.CBOWModel(FLAGS.user_vocab_size, 
    FLAGS.item_vocab_size, FLAGS.size, 
    FLAGS.batch_size, FLAGS.learning_rate,
    FLAGS.learning_rate_decay_factor, u_attributes, i_attributes,
    item_ind2logit_ind, logit_ind2item_ind, loss_function=loss, 
    n_input_items=FLAGS.ni, use_sep_item=FLAGS.use_sep_item,
    dropout=FLAGS.keep_prob, top_N_items=FLAGS.top_N_items, 
    output_feat=FLAGS.output_feat, 
    n_sampled=n_sampled)

  if not os.path.isdir(FLAGS.train_dir):
    os.mkdir(FLAGS.train_dir)
  ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
  # if FLAGS.recommend and ckpt:
  #   # print("%s" % ckpt.model_checkpoint_path)
  #   # if FLAGS.model_option == 'loss':
  #   #   f = os.path.join(FLAGS.train_dir, 'go.ckpt-best')
  #   # elif FLAGS.model_option == 'auc':
  #   #   f = os.path.join(FLAGS.train_dir, 'go.ckpt-best_auc')
  #   # else:
  #   #   print("no such models %s" % FLAGS.model_option)
  #   #   exit()
  #   # ckpt.model_checkpoint_path = f
  #   print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
  #   logging.info("Reading model parameters from %s" % ckpt.model_checkpoint_path)
  #   model.saver.restore(session, ckpt.model_checkpoint_path)

  # # elif ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
  # elif ckpt:
  if ckpt:
    print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    logging.info("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Created model with fresh parameters.")
    logging.info("Created model with fresh parameters.")
    session.run(tf.initialize_all_variables())
  return model

def train():
  with tf.Session(config=tf.ConfigProto(allow_soft_placement=True, 
    log_device_placement=FLAGS.device_log)) as sess:
    run_options = None
    run_metadata = None
    if FLAGS.profile:
      run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
      run_metadata = tf.RunMetadata()
      FLAGS.steps_per_checkpoint = 30
    
    mylog("reading data")
    task = Task(FLAGS.dataset)

    (seq_tr, items_dev, data_tr, data_va, u_attributes, i_attributes,
      item_ind2logit_ind, logit_ind2item_ind, end_ind) = read_data(task)

    power = FLAGS.power
    item_pop, p_item = item_frequency(data_tr, power)

    if FLAGS.use_more_train:
      item_population = range(len(item_ind2logit_ind))
    else:
      item_population = item_pop

    model = create_model(sess, u_attributes, i_attributes, item_ind2logit_ind,
      logit_ind2item_ind, loss=FLAGS.loss, ind_item=item_population)

    # data iterators
    dite = DataIterator(seq_tr, end_ind, FLAGS.batch_size, FLAGS.ni, 
      FLAGS.skip_window, False)
    ite = dite.get_next3()
    mylog('started training')
    step_time, loss, current_step, auc = 0.0, 0.0, 0, 0.0
    
    repeat = 5 if FLAGS.loss.startswith('bpr') else 1   
    patience = FLAGS.patience

    if os.path.isfile(os.path.join(FLAGS.train_dir, 'auc_train.npy')):
      auc_train = list(np.load(os.path.join(FLAGS.train_dir, 'auc_train.npy')))
      auc_dev = list(np.load(os.path.join(FLAGS.train_dir, 'auc_dev.npy')))
      previous_losses = list(np.load(os.path.join(FLAGS.train_dir, 
        'loss_train.npy')))
      losses_dev = list(np.load(os.path.join(FLAGS.train_dir, 'loss_dev.npy')))
      best_auc = max(auc_dev)
      best_loss = min(losses_dev)
    else:
      previous_losses, auc_train, auc_dev, losses_dev = [], [], [], []
      best_auc, best_loss = -1, 1000000

    item_sampled, item_sampled_id2idx = None, None

    train_total_size = float(len(data_tr))
    n_epoch = FLAGS.n_epoch
    steps_per_epoch = int(1.0 * train_total_size / FLAGS.batch_size)
    total_steps = steps_per_epoch * n_epoch

    mylog("Train:")
    mylog("total: {}".format(train_total_size))
    mylog("Steps_per_epoch: {}".format(steps_per_epoch))
    mylog("Total_steps:{}".format(total_steps))
    mylog("Dev:")
    mylog("total: {}".format(len(data_va)))

    while True:

      start_time = time.time()
      # generate batch of training
      (user_input, input_items, output_items) = ite.next()
      if current_step < 5:
        mylog("current step is {}".format(current_step))
        mylog('user')
        mylog(user_input)
        mylog('input_item')
        mylog(input_items)      
        mylog('output_item')
        mylog(output_items)
      

      if FLAGS.loss in ['mw', 'mce'] and current_step % FLAGS.n_resample == 0:
        item_sampled, item_sampled_id2idx = sample_items(item_population, 
          FLAGS.n_sampled, p_item)
      else:
        item_sampled = None

      step_loss = model.step(sess, user_input, input_items, 
        output_items, item_sampled, item_sampled_id2idx, loss=FLAGS.loss,run_op=run_options, run_meta=run_metadata)
      # step_loss = 0

      step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
      loss += step_loss / FLAGS.steps_per_checkpoint
      # auc += step_auc / FLAGS.steps_per_checkpoint
      current_step += 1
      if current_step > total_steps:
        mylog("Training reaches maximum steps. Terminating...")
        break

      if current_step % FLAGS.steps_per_checkpoint == 0:

        if FLAGS.loss in ['ce', 'mce']:
          perplexity = math.exp(loss) if loss < 300 else float('inf')
          mylog("global step %d learning rate %.4f step-time %.4f perplexity %.2f" % (model.global_step.eval(), model.learning_rate.eval(), step_time, perplexity))
        else:
          mylog("global step %d learning rate %.4f step-time %.4f loss %.3f" % (model.global_step.eval(), model.learning_rate.eval(), step_time, loss))
        if FLAGS.profile:
          # Create the Timeline object, and write it to a json
          tl = timeline.Timeline(run_metadata.step_stats)
          ctf = tl.generate_chrome_trace_format()
          with open('timeline.json', 'w') as f:
              f.write(ctf)
          exit()

        # Decrease learning rate if no improvement was seen over last 3 times.
        if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
          sess.run(model.learning_rate_decay_op) 
        previous_losses.append(loss)
        auc_train.append(auc)
        step_time, loss, auc = 0.0, 0.0, 0.0

        if not FLAGS.eval:
          continue
        # # Save checkpoint and zero timer and loss.
        # checkpoint_path = os.path.join(FLAGS.train_dir, "go.ckpt")
        # current_model = model.saver.save(sess, checkpoint_path, 
        #   global_step=model.global_step)
        
        # Run evals on development set and print their loss/auc.
        l_va = len(data_va)
        eval_loss, eval_auc = 0.0, 0.0
        count_va = 0
        start_time = time.time()
        for idx_s in range(0, l_va, FLAGS.batch_size):
          idx_e = idx_s + FLAGS.batch_size
          if idx_e > l_va:
            break
          lt = data_va[idx_s:idx_e]
          user_va = [x[0] for x in lt]
          item_va_input = [items_dev[x[0]] for x in lt]
          item_va_input = map(list, zip(*item_va_input))
          item_va = [x[1] for x in lt]
          
          the_loss = 'warp' if FLAGS.loss == 'mw' else FLAGS.loss
          eval_loss0 = model.step(sess, user_va, item_va_input, item_va, 
            forward_only=True, loss=the_loss)
          eval_loss += eval_loss0
          count_va += 1
        eval_loss /= count_va
        eval_auc /= count_va
        step_time = (time.time() - start_time) / count_va
        if FLAGS.loss in ['ce', 'mce']:
          eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
          mylog("  dev: perplexity %.2f eval_auc %.4f step-time %.4f" % (
            eval_ppx, eval_auc, step_time))
        else:
          mylog("  dev: loss %.3f eval_auc %.4f step-time %.4f" % (eval_loss, 
            eval_auc, step_time))
        sys.stdout.flush()
        
        if eval_loss < best_loss and not FLAGS.test:
          best_loss = eval_loss
          patience = FLAGS.patience

          # Save checkpoint and zero timer and loss.
          checkpoint_path = os.path.join(FLAGS.train_dir, "best.ckpt")
          model.saver.save(sess, checkpoint_path, 
            global_step=0, write_meta_graph = False)
          mylog('Saving best model...')

        if FLAGS.test:
          checkpoint_path = os.path.join(FLAGS.train_dir, "best.ckpt")
          model.saver.save(sess, checkpoint_path, 
            global_step=0, write_meta_graph = False)
          mylog('Saving current model...')

        if eval_loss > best_loss: # and eval_auc < best_auc:
          patience -= 1

        auc_dev.append(eval_auc)
        losses_dev.append(eval_loss)

        if patience < 0 and not FLAGS.test:
          mylog("no improvement for too long.. terminating..")
          mylog("best auc %.4f" % best_auc)
          mylog("best loss %.4f" % best_loss)
          sys.stdout.flush()
          break
  return

def recommend():

  with tf.Session(config=tf.ConfigProto(allow_soft_placement=True, 
    log_device_placement=FLAGS.device_log)) as sess:
    print("reading data")
    task = Task(FLAGS.dataset)
    (_, items_dev, _, _, u_attributes, i_attributes, item_ind2logit_ind, 
      logit_ind2item_ind, _) = read_data(task)
    
    Evaluate = task.get_evaluation()
    evaluation = Evaluate(logit_ind2item_ind, ta=FLAGS.ta, test=FLAGS.test)
    
    model = create_model(sess, u_attributes, i_attributes, item_ind2logit_ind,
      logit_ind2item_ind, loss=FLAGS.loss, ind_item=None)

    Uinds = evaluation.get_uinds()
    N = len(Uinds)
    print("N = %d" % N)
    Uinds = [p for p in Uinds if p in items_dev]
    mylog("new N = {}, (reduced from original {})".format(len(Uinds), N))
    if len(Uinds) < N:
      evaluation.set_uinds(Uinds)
    N = len(Uinds)
    rec = np.zeros((N, FLAGS.top_N_items), dtype=int)
    count = 0
    time_start = time.time()
    for idx_s in range(0, N, FLAGS.batch_size):
      count += 1
      if count % 100 == 0:
        print("idx: %d, c: %d" % (idx_s, count))
        
      idx_e = idx_s + FLAGS.batch_size
      if idx_e <= N:
        users = Uinds[idx_s: idx_e]
        items_input = [items_dev[u] for u in users]
        items_input = map(list, zip(*items_input))
        recs = model.step(sess, users, items_input, forward_only=True, 
          recommend = True, recommend_new = FLAGS.recommend_new)
        rec[idx_s:idx_e, :] = recs
      else:
        users = range(idx_s, N) + [0] * (idx_e - N)
        users = [Uinds[t] for t in users]
        items_input = [items_dev[u] for u in users]
        items_input = map(list, zip(*items_input))
        recs = model.step(sess, users, items_input, forward_only=True, 
          recommend = True, recommend_new = FLAGS.recommend_new)
        idx_e = N
        rec[idx_s:idx_e, :] = recs[:(idx_e-idx_s),:]
    # return rec: i:  uinds[i] --> logid

    time_end = time.time()
    mylog("Time used %.1f" % (time_end - time_start))

    R = evaluation.gen_rec(rec, FLAGS.recommend_new)

    evaluation.eval_on(R)
    s1, s2, s3, s4, s5 = evaluation.get_scores()
    mylog('scores: \n\t{}\n\t{}\n\t{}\n'.format(s1, s2, s3))
    mylog("SCORE_FORMAT: {} {} {}".format(s1[0], s2[0], s3[0]))
    mylog("METRIC_FORMAT1: {}".format(s4))
    mylog("METRIC_FORMAT2: {}".format(s5))
  return

def main(_):
  
  # logging.debug('This message should go to the log file')
  # logging.info('So should this')
  # logging.warning('And this, too')
  if FLAGS.test:
    if FLAGS.data_dir[-1] == '/':
      FLAGS.data_dir = FLAGS.data_dir[:-1] + '_test'
    else:
      FLAGS.data_dir = FLAGS.data_dir + '_test'

  if not os.path.exists(FLAGS.train_dir):
    os.mkdir(FLAGS.train_dir)
  if not FLAGS.recommend:
    log_path = os.path.join(FLAGS.train_dir,"log.txt")
    logging.basicConfig(filename=log_path,level=logging.DEBUG)
    train()
  else:
    log_path = os.path.join(FLAGS.train_dir,"log.recommend.txt")
    logging.basicConfig(filename=log_path,level=logging.DEBUG)
    recommend()
  return

if __name__ == "__main__":
    tf.app.run()

