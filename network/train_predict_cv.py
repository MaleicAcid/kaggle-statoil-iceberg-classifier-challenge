from __future__ import print_function

import os
os.environ['KERAS_BACKEND'] = 'tensorflow'
os.environ['CUDA_VISIBLE_DEVICES'] = '0'

import numpy as np
import pandas as pd

import params
from utils import get_data, get_best_history
from utils import get_data_generator, get_data_generator_test
from utils import get_object_size, get_stats, resize_data

from sklearn.model_selection import train_test_split
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import log_loss, accuracy_score
from keras.callbacks import EarlyStopping, ReduceLROnPlateau, ModelCheckpoint
from keras.preprocessing.image import ImageDataGenerator

epochs = params.max_epochs
batch_size = params.batch_size
validation_split = params.validation_split
best_weights_path = params.best_weights_path
best_model_path = params.best_model_path
random_seed = params.seed
num_folds = params.num_folds
tta_steps = params.tta_steps
model_input_size = params.model_input_size

train = pd.read_json('../data/train.json')
test = pd.read_json('../data/test.json')

train.loc[train['inc_angle'] == "na", 'inc_angle'] = \
    train[train['inc_angle'] != "na"]['inc_angle'].mean()

train['size_1'] = train['band_1'].apply(get_object_size)
test['size_1'] = test['band_1'].apply(get_object_size)

train = get_stats(train)
test = get_stats(test)

# Get prepared data based on band_1, band_2 and meta information
X_train, M_train = get_data(train.band_1.values, train.band_2.values, 
    train.inc_angle.values, train.size_1.values, train.min_1.values,
    train.max_1.values, train.med_1.values, train.mean_1.values,
    train.max_2.values)
X_test, M_test = get_data(test.band_1.values, test.band_2.values,
    test.inc_angle.values, test.size_1.values, test.min_1.values,
    test.max_1.values, test.med_1.values, test.mean_1.values,
    test.max_2.values)
y_train = train['is_iceberg']

if X_train.shape[1:] != model_input_size:
    X_train = resize_data(X_train, model_input_size)
    X_test = resize_data(X_test, model_input_size)


def get_callbacks():
    return [
        EarlyStopping(monitor='val_loss', patience=80, verbose=1, min_delta=1e-4, mode='min'),
        ReduceLROnPlateau(monitor='val_loss', patience=40, factor=0.1, 
            verbose=1, epsilon=1e-4, mode='min'),
        ModelCheckpoint(monitor='val_loss', filepath=best_weights_path, 
            save_best_only=True, save_weights_only=True, mode='min')
    ]


datagen = ImageDataGenerator(
    horizontal_flip=True,
    vertical_flip=True,
    rotation_range=10,
    width_shift_range=0.0,
    height_shift_range=0.0,
    channel_shift_range=0.0,
    shear_range=0.0,
    zoom_range=0.2
)

datagen_test = ImageDataGenerator(
    horizontal_flip=True,
    vertical_flip=True,
    rotation_range=10,
    width_shift_range=0.0,
    height_shift_range=0.0,
    channel_shift_range=0.0,
    shear_range=0.0,
    zoom_range=0.0
)


model_info = params.model_factory(input_shape=X_train.shape[1:], inputs_meta=M_train.shape[1])
model_info.summary()

with open(best_model_path, "w") as json_file:
    json_file.write(model_info.to_json())


def train_and_evaluate_model(model, X_tr, y_tr, X_cv, y_cv):
    xtr, mtr = X_tr
    xcv, mcv = X_cv

    hist = model.fit_generator(
        get_data_generator(datagen, xtr, mtr, ytr, batch_size=batch_size),
        steps_per_epoch=np.ceil(float(len(xtr)) / float(batch_size)),
        epochs=epochs,
        verbose=2,
        validation_data=get_data_generator(datagen, xcv, mcv, ycv, batch_size=batch_size),
        validation_steps=np.ceil(float(len(xcv)) / float(batch_size)),
        callbacks=get_callbacks()
    )

    best_epoch, loss, acc, val_loss, val_acc = get_best_history(hist.history, monitor='val_loss', mode='min')
    print ()
    print ("Best epoch: {}".format(best_epoch))
    print ("loss: {:0.6f} - acc: {:0.4f} - val_loss: {:0.6f} - val_acc: {:0.4f}".format(loss, acc, val_loss, val_acc))
    print ()


def predict_with_tta(model, X_data, M_data, verbose=0):
    predictions = np.zeros((tta_steps, len(X_data)))
    test_probas = model.predict([X_data, M_data], batch_size=batch_size, verbose=verbose)
    predictions[0] = test_probas.reshape(test_probas.shape[0])

    for i in range(1, tta_steps):
        test_probas = model.predict_generator(
            get_data_generator_test(datagen_test, X_data, M_data, batch_size=batch_size),
            steps=np.ceil(float(len(X_data)) / float(batch_size)),
            verbose=verbose
        )
        predictions[i] = test_probas.reshape(test_probas.shape[0])

    return predictions.mean(axis=0)


## ========================= RUN KERAS K-FOLD TRAINING ========================= ##
predictions = np.zeros((num_folds, len(X_test)))
tr_labels, tr_preds, cv_labels, cv_preds = [], [], [], []

skf = StratifiedKFold(n_splits=num_folds, random_state=random_seed, shuffle=True)
for j, (train_index, cv_index) in enumerate(skf.split(X_train, y_train)):
    print ('\n===================FOLD=', j + 1)
    xtr, mtr, ytr = X_train[train_index], M_train[train_index], y_train[train_index]
    xcv, mcv, ycv = X_train[cv_index], M_train[cv_index], y_train[cv_index]

    tr_labels.extend(ytr)
    cv_labels.extend(ycv)

    model = None
    model = params.model_factory(input_shape=X_train.shape[1:], inputs_meta=M_train.shape[1])
    train_and_evaluate_model(model, [xtr, mtr], ytr, [xcv, mcv], ycv)
    model.load_weights(filepath=best_weights_path)

    # Measure train and validation quality
    print ('\nValidating accuracy on training data ...')
    tr_preds.extend(predict_with_tta(model, xtr, mtr))
    cv_preds.extend(predict_with_tta(model, xcv, mcv))

    print ('\nPredicting test data with augmentation ...')
    fold_predictions = predict_with_tta(model, X_test, M_test, verbose=1)
    predictions[j] = fold_predictions

tr_loss = log_loss(tr_labels, tr_preds)
tr_acc = accuracy_score(tr_labels, np.asarray(tr_preds) > 0.5)
val_loss = log_loss(cv_labels, cv_preds)
val_acc = accuracy_score(cv_labels, np.asarray(cv_preds) > 0.5)

print ()
print ("Overall score: ")
print ("train_loss: {:0.6f} - train_acc: {:0.4f} - val_loss: {:0.6f} - val_acc: {:0.4f}".format(
    tr_loss, tr_acc, val_loss, val_acc))
print ()


submission = pd.DataFrame()
submission['id'] = test['id']
submission['is_iceberg'] = predictions.mean(axis=0)
submission.to_csv('../submits/submission.csv', index=False)
