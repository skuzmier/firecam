# Copyright 2018 The Fuego Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
@author: Kinshuk Govil

This is the main code for reading images from webcams and detecting fires

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import settings
sys.path.insert(0, settings.fuegoRoot + '/lib')
import collect_args
import rect_to_squares
import goog_helper
import tf_helper
import db_manager
import email_helper
import img_archive

import logging
import os
import pathlib
import tempfile
import shutil
import time, datetime
import random
import re
from urllib.request import urlretrieve
import tensorflow as tf
from PIL import Image, ImageFile, ImageDraw, ImageFont
ImageFile.LOAD_TRUNCATED_IMAGES = True


def getNextImage(dbManager, cameras, cameraID=None):
    """Gets the next image to check for smoke

    Uses a shared counter being updated by all cooperating detection processes
    to index into the list of cameras to download the image to a local
    temporary directory

    Args:
        dbManager (DbManager):
        cameras (list): list of cameras

    Returns:
        Tuple containing camera name, current timestamp, and filepath of the image
    """
    if getNextImage.tmpDir == None:
        getNextImage.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getNextImage.tmpDir.name)

    if cameraID:
        camera = list(filter(lambda x: x['name'] == cameraID, cameras))[0]
    else:
        index = dbManager.getNextSourcesCounter() % len(cameras)
        camera = cameras[index]
    timestamp = int(time.time())
    imgPath = img_archive.getImgPath(getNextImage.tmpDir.name, camera['name'], timestamp)
    # logging.warning('urlr %s %s', camera['url'], imgPath)
    try:
        urlretrieve(camera['url'], imgPath)
    except Exception as e:
        logging.error('Error fetching image from %s %s', camera['name'], str(e))
        return getNextImage(dbManager, cameras)
    return (camera['name'], timestamp, imgPath)
getNextImage.tmpDir = None

# XXXXX Use a fixed stable directory for testing
# from collections import namedtuple
# Tdir = namedtuple('Tdir', ['name'])
# getNextImage.tmpDir = Tdir('c:/tmp/dftest')


def getNextImageFromDir(imgDirectory):
    """Gets the next image to check for smoke from given directory

    A variant of getNextImage() above but works with files already present
    on the locla filesystem.

    Args:
        imgDirectory (str): directory containing the files

    Returns:
        Tuple containing camera name, current timestamp, and filepath of the image
    """
    if getNextImageFromDir.tmpDir == None:
        getNextImageFromDir.tmpDir = tempfile.TemporaryDirectory()
        logging.warning('TempDir %s', getNextImageFromDir.tmpDir.name)
    if not getNextImageFromDir.files:
        allFiles = os.listdir(imgDirectory)
        # filter out files with _Score suffix because they contain annotated scores
        # generated by drawFireBox() function below.
        getNextImageFromDir.files = list(filter(lambda x: '_Score.jpg' not in x, allFiles))
    getNextImageFromDir.index += 1
    if getNextImageFromDir.index < len(getNextImageFromDir.files):
        fileName = getNextImageFromDir.files[getNextImageFromDir.index]
        origPath = os.path.join(imgDirectory, fileName)
        destPath = os.path.join(getNextImageFromDir.tmpDir.name, fileName)
        shutil.copyfile(origPath, destPath)
        parsed = img_archive.parseFilename(fileName)
        if not parsed:
            # failed to parse, so skip to next image
            return getNextImageFromDir(imgDirectory)
        return (parsed['cameraID'], parsed['unixTime'], destPath)
    logging.warning('Finished processing all images in directory. Exiting')
    exit(0)
getNextImageFromDir.files = None
getNextImageFromDir.index = -1
getNextImageFromDir.tmpDir = None


def segmentImage(imgPath):
    """Segment the given image into sections to for smoke classificaiton

    Args:
        imgPath (str): filepath of the image

    Returns:
        List of dictionary containing information on each segment
    """
    img = Image.open(imgPath)
    ppath = pathlib.PurePath(imgPath)
    segments = rect_to_squares.cutBoxes(img, str(ppath.parent), imgPath)
    img.close()
    return segments


def recordScores(dbManager, camera, timestamp, segments, minusMinutes):
    """Record the smoke scores for each segment into SQL DB

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        segments (list): List of dictionary containing information on each segment
    """
    dt = datetime.datetime.fromtimestamp(timestamp)
    secondsInDay = (dt.hour * 60 + dt.minute) * 60 + dt.second

    for segmentInfo in segments:
        dbRow = {
            'CameraName': camera,
            'Timestamp': timestamp,
            'MinX': segmentInfo['MinX'],
            'MinY': segmentInfo['MinY'],
            'MaxX': segmentInfo['MaxX'],
            'MaxY': segmentInfo['MaxY'],
            'Score': segmentInfo['score'],
            'MinusMinutes': minusMinutes,
            'SecondsInDay': secondsInDay
        }
        dbManager.add_data('scores', dbRow, commit=False)
    dbManager.commit()


def postFilter(dbManager, camera, timestamp, segments):
    """Post classification filter to reduce false positives

    Many times smoke classification scores segments with haze and glare
    above 0.5.  Haze and glare occur tend to occur at similar time over
    multiple days, so this filter raises the threshold based on the max
    smoke score for same segment at same time of day over the last few days.
    Score must be > halfway between max value and 1.  Also, minimum .1 above max.

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        segments (list): Sorted List of dictionary containing information on each segment

    Returns:
        Dictionary with information for the segment most likely to be smoke
        or None
    """
    # segments is sorted, so skip all work if max score is < .5
    if segments[0]['score'] < .5:
        return None

    sqlTemplate = """SELECT MinX,MinY,MaxX,MaxY,count(*) as cnt, avg(score) as avgs, max(score) as maxs FROM scores
    WHERE CameraName='%s' and Timestamp > %s and Timestamp < %s and SecondsInDay > %s and SecondsInDay < %s
    GROUP BY MinX,MinY,MaxX,MaxY"""

    dt = datetime.datetime.fromtimestamp(timestamp)
    secondsInDay = (dt.hour * 60 + dt.minute) * 60 + dt.second
    sqlStr = sqlTemplate % (camera, timestamp - 60*60*int(24*3.5), timestamp - 60*60*12, secondsInDay - 60*60, secondsInDay + 60*60)
    # print('sql', sqlStr, timestamp)
    dbResult = dbManager.query(sqlStr)
    # if len(dbResult) > 0:
    #     print('post filter result', dbResult)
    maxFireSegment = None
    maxFireScore = 0
    for segmentInfo in segments:
        if segmentInfo['score'] < .5: # segments is sorted. we've reached end of segments >= .5
            break
        for row in dbResult:
            if (row['minx'] == segmentInfo['MinX'] and row['miny'] == segmentInfo['MinY'] and
                row['maxx'] == segmentInfo['MaxX'] and row['maxy'] == segmentInfo['MaxY']):
                threshold = (row['maxs'] + 1)/2 # threshold is halfway between max and 1
                threshold = max(threshold, row['maxs'] + 0.1) # threshold at least .1 above max
                # print('thresh', row['minx'], row['miny'], row['maxx'], row['maxy'], row['maxs'], threshold)
                if (segmentInfo['score'] > threshold) and (segmentInfo['score'] > maxFireScore):
                    maxFireScore = segmentInfo['score']
                    maxFireSegment = segmentInfo
                    maxFireSegment['HistAvg'] = row['avgs']
                    maxFireSegment['HistMax'] = row['maxs']
                    maxFireSegment['HistNumSamples'] = row['cnt']

    return maxFireSegment


def collectPositves(service, imgPath, origImgPath, segments):
    """Collect all positive scoring segments

    Copy the images for all segments that score highter than > .5 to google drive folder
    settings.positivePictures. These will be used to train future models.
    Also, copy the full image for reference.

    Args:
        imgPath (str): path name for main image
        segments (list): List of dictionary containing information on each segment
    """
    positiveSegments = 0
    ppath = pathlib.PurePath(origImgPath)
    imgNameNoExt = str(os.path.splitext(ppath.name)[0])
    origImg = None
    for segmentInfo in segments:
        if segmentInfo['score'] > .5:
            if imgPath != origImgPath:
                if not origImg:
                    origImg = Image.open(origImgPath)
                cropCoords = (segmentInfo['MinX'], segmentInfo['MinY'], segmentInfo['MaxX'], segmentInfo['MaxY'])
                croppedOrigImg = origImg.crop(cropCoords)
                cropImgName = imgNameNoExt + '_Crop_' + 'x'.join(list(map(lambda x: str(x), cropCoords))) + '.jpg'
                cropImgPath = os.path.join(str(ppath.parent), cropImgName)
                croppedOrigImg.save(cropImgPath, format='JPEG')
                croppedOrigImg.close()
                if hasattr(settings, 'positivePicturesDir'):
                    destPath = os.path.join(settings.positivePicturesDir, cropImgName)
                    shutil.copy(cropImgPath, destPath)
                else:
                    goog_helper.uploadFile(service, settings.positivePictures, cropImgPath)
                os.remove(cropImgPath)
            if hasattr(settings, 'positivePicturesDir'):
                pp = pathlib.PurePath(segmentInfo['imgPath'])
                destPath = os.path.join(settings.positivePicturesDir, pp.name)
                shutil.copy(segmentInfo['imgPath'], destPath)
            else:
                goog_helper.uploadFile(service, settings.positivePictures, segmentInfo['imgPath'])
            positiveSegments += 1

    if positiveSegments > 0:
        # Commenting out saving full images for now to reduce data
        # goog_helper.uploadFile(service, settings.positivePictures, imgPath)
        logging.warning('Found %d positives in image %s', positiveSegments, ppath.name)


def drawRect(imgDraw, x0, y0, x1, y1, width, color):
    for i in range(width):
        imgDraw.rectangle((x0+i,y0+i,x1-i,y1-i),outline=color)


def drawFireBox(imgPath, fireSegment):
    """Draw bounding box with fire detection with score on image

    Stores the resulting annotated image as new file

    Args:
        imgPath (str): filepath of the image

    Returns:
        filepath of new image file
    """
    img = Image.open(imgPath)
    imgDraw = ImageDraw.Draw(img)
    x0 = fireSegment['MinX']
    y0 = fireSegment['MinY']
    x1 = fireSegment['MaxX']
    y1 = fireSegment['MaxY']
    centerX = (x0 + x1)/2
    centerY = (y0 + y1)/2
    color = "red"
    lineWidth=3
    drawRect(imgDraw, x0, y0, x1, y1, lineWidth, color)

    fontSize=80
    font = ImageFont.truetype(settings.fuegoRoot + '/lib/Roboto-Regular.ttf', size=fontSize)
    scoreStr = '%.2f' % fireSegment['score']
    textSize = imgDraw.textsize(scoreStr, font=font)
    imgDraw.text((centerX - textSize[0]/2, centerY - textSize[1]), scoreStr, font=font, fill=color)

    color = "blue"
    fontSize=70
    font = ImageFont.truetype(settings.fuegoRoot + '/lib/Roboto-Regular.ttf', size=fontSize)
    scoreStr = '%.2f' % fireSegment['HistMax']
    textSize = imgDraw.textsize(scoreStr, font=font)
    imgDraw.text((centerX - textSize[0]/2, centerY), scoreStr, font=font, fill=color)

    filePathParts = os.path.splitext(imgPath)
    annotatedFile = filePathParts[0] + '_Score' + filePathParts[1]
    img.save(annotatedFile, format="JPEG")
    del imgDraw
    img.close()
    return annotatedFile


def recordDetection(dbManager, service, camera, timestamp, imgPath, annotatedFile, fireSegment):
    """Record that a smoke/fire has been detected

    Record the detection with useful metrics in 'detections' table in SQL DB.
    Also, upload image file to google drive

    Args:
        dbManager (DbManager):
        service:
        camera (str): camera name
        timestamp (int):
        imgPath: filepath of the image
        annotatedFile: filepath of the image with annotated box and score
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke

    Returns:
        List of Google drive IDs for the uploaded image files
    """
    logging.warning('Fire detected by camera %s, image %s, segment %s', camera, imgPath, str(fireSegment))
    # upload file to google drive detection dir
    driveFileIDs = []
    driveFile = goog_helper.uploadFile(service, settings.detectionPictures, imgPath)
    if driveFile:
        driveFileIDs.append(driveFile['id'])
    driveFile = goog_helper.uploadFile(service, settings.detectionPictures, annotatedFile)
    if driveFile:
        driveFileIDs.append(driveFile['id'])
    logging.warning('Uploaded to google drive detections folder %s', str(driveFileIDs))

    dbRow = {
        'CameraName': camera,
        'Timestamp': timestamp,
        'MinX': fireSegment['MinX'],
        'MinY': fireSegment['MinY'],
        'MaxX': fireSegment['MaxX'],
        'MaxY': fireSegment['MaxY'],
        'Score': fireSegment['score'],
        'HistAvg': fireSegment['HistAvg'],
        'HistMax': fireSegment['HistMax'],
        'HistNumSamples': fireSegment['HistNumSamples'],
        'ImageID': driveFileIDs[0] if driveFileIDs else ''
    }
    dbManager.add_data('detections', dbRow)
    return driveFileIDs


def checkAndUpdateAlerts(dbManager, camera, timestamp, driveFileIDs):
    """Check if alert has been recently sent out for given camera

    If an alert of this camera has't been recorded recently, record this as an alert

    Args:
        dbManager (DbManager):
        camera (str): camera name
        timestamp (int):
        driveFileIDs (list): List of Google drive IDs for the uploaded image files

    Returns:
        True if this is a new alert, False otherwise
    """
    sqlTemplate = """SELECT * FROM alerts
    where CameraName='%s' and timestamp > %s"""
    sqlStr = sqlTemplate % (camera, timestamp - 60*60*12) # suppress alerts for 12 hours
    dbResult = dbManager.query(sqlStr)
    if len(dbResult) > 0:
        logging.warning('Supressing new alert due to recent alert')
        return False

    dbRow = {
        'CameraName': camera,
        'Timestamp': timestamp,
        'ImageID': driveFileIDs[0] if driveFileIDs else ''
    }
    dbManager.add_data('alerts', dbRow)
    return True


def alertFire(camera, imgPath, annotatedFile, driveFileIDs, fireSegment):
    """Send an email alert for a potential new fire

    Send email with information about the camera and fire score includeing
    image attachments

    Args:
        camera (str): camera name
        imgPath: filepath of the original image
        annotatedFile: filepath of the annotated image
        driveFileIDs (list): List of Google drive IDs for the uploaded image files
        fireSegment (dictionary): dictionary with information for the segment with fire/smoke
    """
    # send email
    fromAccount = (settings.fuegoEmail, settings.fuegoPasswd)
    subject = 'Possible (%d%%) fire in camera %s' % (int(fireSegment['score']*100), camera)
    body = 'Please check the attached image for fire.'
    for driveFileID in driveFileIDs:
        driveTempl = '\nAlso available from google drive as https://drive.google.com/file/d/%s'
        driveBody = driveTempl % driveFileID
        body += driveBody
    email_helper.send_email(fromAccount, settings.detectionsEmail, subject, body, [imgPath, annotatedFile])


def deleteImageFiles(imgPath, origImgPath, annotatedFile, segments):
    """Delete all image files given in segments

    Args:
        imgPath: filepath of the original image
        annotatedFile: filepath of the annotated image
        segments (list): List of dictionary containing information on each segment
    """
    for segmentInfo in segments:
        os.remove(segmentInfo['imgPath'])
    os.remove(imgPath)
    if imgPath != origImgPath:
        os.remove(origImgPath)
    if annotatedFile:
        os.remove(annotatedFile)
    ppath = pathlib.PurePath(imgPath)
    # leftoverFiles = os.listdir(str(ppath.parent))
    # if len(leftoverFiles) > 0:
    #     logging.warning('leftover files %s', str(leftoverFiles))


def getLastScoreCamera(dbManager):
    sqlStr = "SELECT CameraName from scores order by Timestamp desc limit 1;"
    dbResult = dbManager.query(sqlStr)
    if len(dbResult) > 0:
        return dbResult[0]['CameraName']
    return None


def heartBeat(filename):
    """Inform monitor process that this detection process is alive

    Informs by updating the timestamp on given file

    Args:
        filename (str): file path of file used for heartbeating
    """
    pathlib.Path(filename).touch()


def segmentAndClassify(imgPath, tfSession, graph, labels):
    segments = segmentImage(imgPath)
    # print('si', segments)
    tf_helper.classifySegments(tfSession, graph, labels, segments)
    segments.sort(key=lambda x: -x['score'])
    return segments


def recordFilterReport(args, dbManager, cameraID, timestamp, imgPath, origImgPath, segments, minusMinutes, googleDrive):
    recordScores(dbManager, cameraID, timestamp, segments, minusMinutes)
    if args.collectPositves:
        collectPositves(googleDrive, imgPath, origImgPath, segments)
    fireSegment = postFilter(dbManager, cameraID, timestamp, segments)
    annotatedFile = None
    if fireSegment:
        annotatedFile = drawFireBox(origImgPath, fireSegment)
        driveFileIDs = recordDetection(dbManager, googleDrive, cameraID, timestamp, origImgPath, annotatedFile, fireSegment)
        if checkAndUpdateAlerts(dbManager, cameraID, timestamp, driveFileIDs):
            alertFire(cameraID, origImgPath, annotatedFile, driveFileIDs, fireSegment)
    deleteImageFiles(imgPath, origImgPath, annotatedFile, segments)
    if (args.heartbeat):
        heartBeat(args.heartbeat)
    logging.warning('Highest score for camera %s: %f' % (cameraID, segments[0]['score']))


def genDiffImage(imgPath, earlierImgPath, minusMinutes):
    imgA = Image.open(imgPath)
    imgB = Image.open(earlierImgPath)
    imgDiff = img_archive.diffImages(imgA, imgB)
    parsedName = img_archive.parseFilename(imgPath)
    parsedName['diffMinutes'] = minusMinutes
    imgDiffName = img_archive.repackFileName(parsedName)
    ppath = pathlib.PurePath(imgPath)
    imgDiffPath = os.path.join(str(ppath.parent), imgDiffName)
    imgDiff.save(imgDiffPath, format='JPEG')
    return imgDiffPath


def expectedDrainSeconds(deferredImages):
    # XXXX should be based on actual rate on randomized order of cameras
    # XXXX but for now, just using 3 seconds as estimate
    return len(deferredImages)*3


def getDeferrredImgToProcess(deferredImages, minusMinutes, currentTime):
    if minusMinutes == 0:
        return None
    if len(deferredImages) == 0:
        return None
    if (expectedDrainSeconds(deferredImages) >= 60*minusMinutes) or (deferredImages[0]['runTime'] < currentTime):
        img = deferredImages[0]
        del deferredImages[0]
        return img
    return None


def main():
    optArgs = [
        ["b", "heartbeat", "filename used for heartbeating check"],
        ["c", "collectPositves", "collect positive segments for training data"],
        ["d", "imgDirectory", "Name of the directory containing the images"],
        ["t", "time", "Time breakdown for processing images"],
        ["m", "minusMinutes", "(optional) subtract images from given number of minutes ago"],
    ]
    args = collect_args.collectArgs([], optionalArgs=optArgs, parentParsers=[goog_helper.getParentParser()])
    minusMinutes = int(args.minusMinutes) if args.minusMinutes else 0
    # commenting out the print below to reduce showing secrets in settings
    # print('Settings:', list(map(lambda a: (a,getattr(settings,a)), filter(lambda a: not a.startswith('__'), dir(settings)))))
    googleServices = goog_helper.getGoogleServices(settings, args)
    if settings.db_file:
        logging.warning('using sqlite %s', settings.db_file)
        dbManager = db_manager.DbManager(sqliteFile=settings.db_file)
    else:
        logging.warning('using postgres %s', settings.psqlHost)
        dbManager = db_manager.DbManager(psqlHost=settings.psqlHost, psqlDb=settings.psqlDb,
                                        psqlUser=settings.psqlUser, psqlPasswd=settings.psqlPasswd)
    cameras = dbManager.get_sources()

    deferredImages = []
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # quiet down tensorflow logging
    graph = tf_helper.load_graph(settings.model_file)
    labels = tf_helper.load_labels(settings.labels_file)
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.1 #hopefully reduces segfaults
    with tf.Session(graph=graph, config=config) as tfSession:
        while True:
            timeStart = time.time()
            deferredImageInfo = getDeferrredImgToProcess(deferredImages, minusMinutes, timeStart)
            if deferredImageInfo:
                # logging.warn('DefImg: %d, %s, %s', len(deferredImages), timeStart, deferredImageInfo)
                (cameraID, timestamp, imgPath) = getNextImage(dbManager, cameras, deferredImageInfo['cameraID'])
            elif args.imgDirectory:
                (cameraID, timestamp, imgPath) = getNextImageFromDir(args.imgDirectory)
            else:
                (cameraID, timestamp, imgPath) = getNextImage(dbManager, cameras)
            timeFetch = time.time()
            classifyImgPath = imgPath
            if minusMinutes and not deferredImageInfo:
                # add image to Q if not already another one from same camera
                matches = list(filter(lambda x: x['cameraID'] == cameraID, deferredImages))
                if len(matches) > 0:
                    assert len(matches) == 1
                    logging.warn('Camera already in list waiting processing %s, %s', timeStart, matches[0])
                    time.sleep(2) # take a nap to let things catch up
                    continue
                deferredImages.append({
                    'runTime': timeStart + 60*minusMinutes,
                    'cameraID': cameraID,
                    'imgPath': imgPath
                })
                logging.warn('Defer camera %s.  Len %d', cameraID, len(deferredImages))
                continue
            if deferredImageInfo:
                imgDiffPath = genDiffImage(imgPath, deferredImageInfo['imgPath'], minusMinutes)
                classifyImgPath = imgDiffPath
                # logging.warn('Diffed image %s', classifyImgPath)

            segments = segmentAndClassify(classifyImgPath, tfSession, graph, labels)
            timeClassify = time.time()
            recordFilterReport(args, dbManager, cameraID, timestamp, classifyImgPath, imgPath, segments, minusMinutes, googleServices['drive'])
            if deferredImageInfo:
                os.remove(deferredImageInfo['imgPath'])
            timePost = time.time()
            if args.time:
                logging.warning('Timings: fetch=%.2f, classify=%.2f, post=%.2f',
                    timeFetch-timeStart, timeClassify-timeFetch, timePost-timeClassify)


if __name__=="__main__":
    main()
