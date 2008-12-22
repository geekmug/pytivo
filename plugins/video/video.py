import transcode, os, socket, re, urllib, zlib
from Cheetah.Template import Template
from plugin import Plugin, quote, unquote
from urlparse import urlparse
from xml.sax.saxutils import escape
from lrucache import LRUCache
from UserDict import DictMixin
from datetime import datetime, timedelta
import config
import time
import mind
import logging

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = 'Video'

# Preload the templates
tcname = os.path.join(SCRIPTDIR, 'templates', 'container.tmpl')
ttname = os.path.join(SCRIPTDIR, 'templates', 'TvBus.tmpl')
txname = os.path.join(SCRIPTDIR, 'templates', 'container.xsl')
CONTAINER_TEMPLATE = file(tcname, 'rb').read()
TVBUS_TEMPLATE = file(ttname, 'rb').read()
XSL_TEMPLATE = file(txname, 'rb').read()

extfile = os.path.join(SCRIPTDIR, 'video.ext')
try:
    extensions = file(extfile).read().split()
except:
    extensions = None

class Video(Plugin):

    CONTENT_TYPE = 'x-container/tivo-videos'

    def pre_cache(self, full_path):
        if Video.video_file_filter(self, full_path):
            transcode.supported_format(full_path)

    def video_file_filter(self, full_path, type=None):
        if os.path.isdir(full_path):
            return True
        if extensions:
            return os.path.splitext(full_path)[1].lower() in extensions
        else:
            return transcode.supported_format(full_path)

    def send_file(self, handler, container, name):
        if (handler.headers.getheader('Range') and
            handler.headers.getheader('Range') != 'bytes=0-'):
            handler.send_response(206)
            handler.send_header('Connection', 'close')
            handler.send_header('Content-Type', 'video/x-tivo-mpeg')
            handler.send_header('Transfer-Encoding', 'chunked')
            handler.end_headers()
            handler.wfile.write("\x30\x0D\x0A")
            return

        tsn = handler.headers.getheader('tsn', '')

        o = urlparse("http://fake.host" + handler.path)
        path = unquote(o[2])
        handler.send_response(200)
        handler.end_headers()
        transcode.output_video(container['path'] + path[len(name) + 1:],
                               handler.wfile, tsn)

    def __isdir(self, full_path):
        return os.path.isdir(full_path)

    def __duration(self, full_path):
        return transcode.video_info(full_path)['millisecs']

    def __total_items(self, full_path):
        count = 0
        try:
            for f in os.listdir(full_path):
                if f.startswith('.'):
                    continue
                f = os.path.join(full_path, f)
                if os.path.isdir(f):
                    count += 1
                elif extensions:
                    if os.path.splitext(f)[1].lower() in extensions:
                        count += 1
                elif f in transcode.info_cache:
                    if transcode.supported_format(f):
                        count += 1
        except:
            pass
        return count

    def __est_size(self, full_path, tsn = ''):
        # Size is estimated by taking audio and video bit rate adding 2%

        if transcode.tivo_compatible(full_path, tsn)[0]:
            # Is TiVo-compatible mpeg2
            return int(os.stat(full_path).st_size)
        else:
            # Must be re-encoded
            if config.getAudioCodec(tsn) == None:
                audioBPS = config.getMaxAudioBR(tsn)*1000
            else:
                audioBPS = config.strtod(config.getAudioBR(tsn))
            videoBPS = transcode.select_videostr(full_path, tsn)
            bitrate =  audioBPS + videoBPS
            return int((self.__duration(full_path) / 1000) *
                       (bitrate * 1.02 / 8))

    def getMetadataFromTxt(self, full_path):
        metadata = {}

        default_meta = os.path.join(os.path.split(full_path)[0], 'default.txt')
        standard_meta = full_path + '.txt'
        subdir_meta = os.path.join(os.path.dirname(full_path), '.meta',
                                   os.path.basename(full_path)) + '.txt'

        for metafile in (default_meta, standard_meta, subdir_meta):
            metadata.update(self.__getMetadataFromFile(metafile))

        return metadata

    def __getMetadataFromFile(self, f):
        metadata = {}

        if os.path.exists(f):
            for line in open(f):
                if line.strip().startswith('#'):
                    continue
                if not ':' in line:
                    continue

                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                if key.startswith('v'):
                    if key in metadata:
                        metadata[key].append(value)
                    else:
                        metadata[key] = [value]
                else:
                    metadata[key] = value

        return metadata

    def metadata_basic(self, full_path):
        metadata = {}

        base_path, title = os.path.split(full_path)
        ctime = os.stat(full_path).st_ctime
        if (ctime < 0): ctime = 0
        originalAirDate = datetime.fromtimestamp(ctime)

        metadata['title'] = '.'.join(title.split('.')[:-1])
        metadata['seriesTitle'] = metadata['title'] # default to the filename
        metadata['originalAirDate'] = originalAirDate.isoformat()

        metadata.update(self.getMetadataFromTxt(full_path))

        return metadata

    def metadata_full(self, full_path, tsn=''):
        metadata = {}

        now = datetime.utcnow()

        duration = self.__duration(full_path)
        duration_delta = timedelta(milliseconds = duration)

        metadata['time'] = now.isoformat()
        metadata['startTime'] = now.isoformat()
        metadata['stopTime'] = (now + duration_delta).isoformat()
        metadata['size'] = self.__est_size(full_path, tsn)
        metadata['duration'] = duration
        vInfo = transcode.video_info(full_path)
        transcode_options = {}
        if not transcode.tivo_compatible(full_path, tsn)[0]:
            transcode_options = transcode.transcode(True, full_path, '', tsn)
        metadata['vHost'] = [str(transcode.tivo_compatible(full_path, tsn)[1])]+\
                            ['SOURCE INFO: ']+["%s=%s" % (k, v) for k, v in sorted(transcode.video_info(full_path).items(), reverse=True)]+\
                            ['TRANSCODE OPTIONS: ']+["%s" % (v) for k, v in transcode_options.items()]+\
                            ['SOURCE FILE: ']+[str(os.path.split(full_path)[1])]
        if not (full_path[-5:]).lower() == '.tivo':
            if ((int(vInfo['vHeight']) >= 720 and
                 config.getTivoHeight >= 720) or
                (int(vInfo['vWidth']) >= 1280 and
                 config.getTivoWidth >= 1280)):
                metadata['showingBits'] = '4096'

        metadata.update(self.metadata_basic(full_path))

        min = duration_delta.seconds / 60
        sec = duration_delta.seconds % 60
        hours = min / 60
        min = min % 60
        metadata['iso_duration'] = 'P' + str(duration_delta.days) + \
                                   'DT' + str(hours) + 'H' + str(min) + \
                                   'M' + str(sec) + 'S'
        return metadata

    def QueryContainer(self, handler, query):
        tsn = handler.headers.getheader('tsn', '')
        subcname = query['Container'][0]
        cname = subcname.split('/')[0]

        if not handler.server.containers.has_key(cname) or \
           not self.get_local_path(handler, query):
            handler.send_response(404)
            handler.end_headers()
            return

        container = handler.server.containers[cname]
        precache = container.get('precache', 'False').lower() == 'true'

        files, total, start = self.get_files(handler, query,
                                             self.video_file_filter)

        videos = []
        local_base_path = self.get_local_base_path(handler, query)
        for f in files:
            mtime = os.stat(f).st_mtime
            if (mtime < 0): mtime = 0
            mtime = datetime.fromtimestamp(mtime)
            video = VideoDetails()
            video['captureDate'] = hex(int(time.mktime(mtime.timetuple())))
            video['name'] = os.path.split(f)[1]
            video['path'] = f
            video['part_path'] = f.replace(local_base_path, '', 1)
            if not video['part_path'].startswith(os.path.sep):
                video['part_path'] = os.path.sep + video['part_path']
            video['title'] = os.path.split(f)[1]
            video['is_dir'] = self.__isdir(f)
            if video['is_dir']:
                video['small_path'] = subcname + '/' + video['name']
                video['total_items'] = self.__total_items(f)
            else:
                if precache or len(files) == 1 or f in transcode.info_cache:
                    video['valid'] = transcode.supported_format(f)
                    if video['valid']:
                        video.update(self.metadata_full(f, tsn))
                else:
                    video['valid'] = True
                    video.update(self.metadata_basic(f))

            videos.append(video)

        handler.send_response(200)
        handler.end_headers()
        t = Template(CONTAINER_TEMPLATE)
        t.container = cname
        t.name = subcname
        t.total = total
        t.start = start
        t.videos = videos
        t.quote = quote
        t.escape = escape
        t.crc = zlib.crc32
        t.guid = config.getGUID()
        t.tivos = handler.tivos
        t.tivo_names = handler.tivo_names
        handler.wfile.write(t)

    def TVBusQuery(self, handler, query):
        tsn = handler.headers.getheader('tsn', '')
        f = query['File'][0]
        path = self.get_local_path(handler, query)
        file_path = path + f

        file_info = VideoDetails()
        file_info['valid'] = transcode.supported_format(file_path)
        if file_info['valid']:
            file_info.update(self.metadata_full(file_path, tsn))

        handler.send_response(200)
        handler.end_headers()
        t = Template(TVBUS_TEMPLATE)
        t.video = file_info
        t.escape = escape
        handler.wfile.write(t)

    def XSL(self, handler, query):
        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(XSL_TEMPLATE)

    def Push(self, handler, query):
        f = unquote(query['File'][0])

        tsn = query['tsn'][0]
        for key in handler.tivo_names:
            if handler.tivo_names[key] == tsn:
                tsn = key
                break

        path = self.get_local_path(handler, query)
        file_path = path + f

        file_info = VideoDetails()
        file_info['valid'] = transcode.supported_format(file_path)
        if file_info['valid']:
            file_info.update(self.metadata_full(file_path, tsn))

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('tivo.com',123))
        ip = s.getsockname()[0]
        container = quote(query['Container'][0].split('/')[0])
        port = config.getPort()

        url = 'http://%s:%s/%s%s' % (ip, port, container, quote(f))

        try:
            m = mind.getMind()
            m.pushVideo(
                tsn = tsn,
                url = url,
                description = file_info['description'],
                duration = file_info['duration'] / 1000,
                size = file_info['size'],
                title = file_info['title'],
                subtitle = file_info['name'])
        except Exception, e:
            import traceback
            handler.send_response(500)
            handler.end_headers()
            handler.wfile.write('%s\n\n%s' % (e, traceback.format_exc() ))
            raise

        referer = handler.headers.getheader('Referer')
        handler.send_response(302)
        handler.send_header('Location', referer)
        handler.end_headers()


class VideoDetails(DictMixin):

    def __init__(self, d=None):
        if d:
            self.d = d
        else:
            self.d = {}

    def __getitem__(self, key):
        if key not in self.d:
            self.d[key] = self.default(key)
        return self.d[key]

    def __contains__(self, key):
        return True

    def __setitem__(self, key, value):
        self.d[key] = value

    def __delitem__(self):
        del self.d[key]

    def keys(self):
        return self.d.keys()

    def __iter__(self):
        return self.d.__iter__()

    def iteritems(self):
        return self.d.iteritems()

    def default(self, key):
        defaults = {
            'showingBits' : '0',
            'episodeNumber' : '0',
            'displayMajorNumber' : '0',
            'displayMinorNumber' : '0',
            'isEpisode' : 'true',
            'colorCode' : ('COLOR', '4'),
            'showType' : ('SERIES', '5'),
            'tvRating' : ('NR', '7')
        }
        if key in defaults:
            return defaults[key]
        elif key.startswith('v'):
            return []
        else:
            return ''
