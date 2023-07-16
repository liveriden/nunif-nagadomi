import av
import math
from tqdm import tqdm
from PIL import Image
import mimetypes
import re


# Add video mimetypes that does not exist in mimetypes
mimetypes.add_type("video/x-ms-asf", ".asf")
mimetypes.add_type("video/x-ms-vob", ".vob")
mimetypes.add_type("video/divx", ".divx")
mimetypes.add_type("video/3gpp", ".3gp")
mimetypes.add_type("video/ogg", ".ogg")
mimetypes.add_type("video/3gpp2", ".3g2")
mimetypes.add_type("video/m2ts", ".m2ts")
mimetypes.add_type("video/m2ts", ".ts")
mimetypes.add_type("video/vnd.rn-realmedia", ".rm")  # fake


VIDEO_EXTENSIONS = [
    ".mp4", ".m4v", ".mkv", ".mpeg", ".mpg", ".mp2", ".avi", ".wmv", ".mov", ".flv", ".webm",
    ".asf", ".vob", ".divx", ".3gp", ".ogg", ".3g2", ".m2ts", ".ts", ".rm",
]


def get_fps(stream):
    return stream.guessed_rate


def guess_frames(stream, fps=None):
    fps = fps or get_fps(stream)
    return math.ceil(float(stream.duration * stream.time_base) * fps)


def get_duration(stream):
    return math.ceil(float(stream.duration * stream.time_base))


def get_frames(stream):
    if stream.frames > 0:
        return stream.frames
    else:
        # frames is unknown
        return guess_frames(stream)


def from_image(im):
    return av.video.frame.VideoFrame.from_image(im)


def _print_len(stream):
    print("frames", stream.frames)
    print("guessed_frames", guess_frames(stream))
    print("duration", get_duration(stream))
    print("base_rate", float(stream.base_rate))
    print("average_rate", float(stream.average_rate))
    print("guessed_rate", float(stream.guessed_rate))


class FixedFPSFilter():
    @staticmethod
    def parse_vf_option(vf):
        video_filters = []
        vf = vf.strip()
        if not vf:
            return video_filters

        for line in re.split(r'(?<!\\),', vf):
            line = line.strip()
            if line:
                col = re.split(r'(?<!\\)=', line, 1)
                if len(col) == 2:
                    filter_name, filter_option = col
                else:
                    filter_name, filter_option = col[0], ""
                filter_name, filter_option = filter_name.strip(), filter_option.strip()
                video_filters.append((filter_name, filter_option))
        return video_filters

    @staticmethod
    def build_graph(graph, template_stream, video_filters):
        buffer = graph.add_buffer(template=template_stream)
        prev_filter = buffer
        for filter_name, filter_option in video_filters:
            new_filter = graph.add(filter_name, filter_option if filter_option else None)
            prev_filter.link_to(new_filter)
            prev_filter = new_filter
        buffersink = graph.add("buffersink")
        prev_filter.link_to(buffersink)
        graph.configure()

    def __init__(self, video_stream, fps, vf=""):
        self.graph = av.filter.Graph()
        video_filters = self.parse_vf_option(vf)
        video_filters.append(("fps", str(fps)))
        self.build_graph(self.graph, video_stream, video_filters)

    def update(self, frame):
        self.graph.push(frame)
        try:
            return self.graph.pull()
        except av.error.BlockingIOError:
            return None
        except av.error.EOFError:
            # finished
            return None


class VideoOutputConfig():
    def __init__(self, pix_fmt="yuv420p", fps=30, options={}):
        self.pix_fmt = pix_fmt
        self.fps = fps
        self.options = options


def default_config_callback(stream):
    fps = get_fps(stream)
    if float(fps) > 30:
        fps = 30
    return VideoOutputConfig(
        fps=fps,
        options={"preset": "ultrafast", "crf": "20"}
    )


def test_output_size(frame_callback, video_stream, vf):
    video_filter = FixedFPSFilter(video_stream, fps=60, vf=vf)
    empty_image = Image.new("RGB", (video_stream.codec_context.width,
                                    video_stream.codec_context.height), (128, 128, 128))
    test_frame = av.video.frame.VideoFrame.from_image(empty_image)
    pts_step = int((1. / video_stream.time_base) / 30) or 1
    test_frame.pts = pts_step
    while True:
        while True:
            frame = video_filter.update(test_frame)
            test_frame.pts = (test_frame.pts + pts_step)
            if frame is not None:
                break
        output_frame = get_new_frames(frame_callback(frame))
        if output_frame:
            output_frame = output_frame[0]
            break
    return output_frame.width, output_frame.height


def get_new_frames(frame_or_frames_or_none):
    if frame_or_frames_or_none is None:
        return []
    elif isinstance(frame_or_frames_or_none, (list, tuple)):
        return frame_or_frames_or_none
    else:
        return [frame_or_frames_or_none]


# TODO: correct colorspace transform


def process_video(input_path, output_path,
                  frame_callback,
                  config_callback=default_config_callback,
                  title=None,
                  vf="",
                  stop_event=None, tqdm_fn=None):
    input_container = av.open(input_path)
    if len(input_container.streams.video) == 0:
        raise ValueError("No video stream")

    video_input_stream = input_container.streams.video[0]
    video_input_stream.thread_type = "AUTO"
    # _print_len(video_input_stream)
    audio_input_stream = audio_output_stream = None
    if len(input_container.streams.audio) > 0:
        # has audio stream
        audio_input_stream = input_container.streams.audio[0]

    config = config_callback(video_input_stream)
    output_container = av.open(output_path, 'w')

    fps_filter = FixedFPSFilter(video_input_stream, config.fps, vf)
    output_size = test_output_size(frame_callback, video_input_stream, vf)
    video_output_stream = output_container.add_stream("libx264", config.fps)
    video_output_stream.thread_type = "AUTO"
    video_output_stream.pix_fmt = config.pix_fmt
    video_output_stream.width = output_size[0]
    video_output_stream.height = output_size[1]
    video_output_stream.options = config.options
    if audio_input_stream is not None:
        if audio_input_stream.rate < 16000:
            audio_output_stream = output_container.add_stream("aac", 16000)
            audio_copy = False
        else:
            try:
                audio_output_stream = output_container.add_stream(template=audio_input_stream)
                audio_copy = True
            except ValueError:
                audio_output_stream = output_container.add_stream("aac", audio_input_stream.rate)
                audio_copy = False

    desc = (title if title else output_path)
    ncols = len(desc) + 60
    tqdm_fn = tqdm_fn or tqdm
    pbar = tqdm_fn(desc=desc, total=guess_frames(video_input_stream, config.fps), ncols=ncols)
    streams = [s for s in [video_input_stream, audio_input_stream] if s is not None]
    for packet in input_container.demux(streams):
        if packet.stream.type == "video":
            for frame in packet.decode():
                frame = fps_filter.update(frame)
                if frame is not None:
                    for new_frame in get_new_frames(frame_callback(frame)):
                        enc_packet = video_output_stream.encode(new_frame)
                        if enc_packet:
                            output_container.mux(enc_packet)
                        pbar.update(1)

        elif packet.stream.type == "audio":
            if packet.dts is not None:
                if audio_copy:
                    packet.stream = audio_output_stream
                    output_container.mux(packet)
                else:
                    for frame in packet.decode():
                        frame.pts = None
                        enc_packet = audio_output_stream.encode(frame)
                        if enc_packet:
                            output_container.mux(enc_packet)
        if stop_event is not None and stop_event.is_set():
            break

    frame = fps_filter.update(None)
    if frame is not None:
        for new_frame in get_new_frames(frame_callback(frame)):
            enc_packet = video_output_stream.encode(new_frame)
            if enc_packet:
                output_container.mux(enc_packet)
                pbar.update(1)

    for new_frame in get_new_frames(frame_callback(None)):
        enc_packet = video_output_stream.encode(new_frame)
        if enc_packet:
            output_container.mux(enc_packet)
            pbar.update(1)

    packet = video_output_stream.encode(None)
    if packet:
        output_container.mux(packet)
    pbar.close()
    output_container.close()
    input_container.close()


def process_video_keyframes(input_path, frame_callback, min_interval_sec=4., title=None, stop_event=None):
    input_container = av.open(input_path)
    if len(input_container.streams.video) == 0:
        raise ValueError("No video stream")

    video_input_stream = input_container.streams.video[0]
    video_input_stream.thread_type = "AUTO"
    video_input_stream.codec_context.skip_frame = "NONKEY"

    max_progress = get_duration(video_input_stream)
    desc = (title if title else input_path)
    ncols = len(desc) + 60
    pbar = tqdm(desc=desc, total=max_progress, ncols=ncols)
    prev_sec = 0
    for frame in input_container.decode(video_input_stream):
        current_sec = math.ceil(frame.pts * video_input_stream.time_base)
        if current_sec - prev_sec >= min_interval_sec:
            frame_callback(frame)
            pbar.update(current_sec - prev_sec)
            prev_sec = current_sec
        if stop_event is not None and stop_event.is_set():
            break
    pbar.close()
    input_container.close()


if __name__ == "__main__":
    from PIL import ImageOps
    import argparse

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input", "-i", type=str, required=True,
                        help="input video file")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="output video file")
    args = parser.parse_args()

    def make_config(stream):
        fps = get_fps(stream)
        if fps > 30:
            fps = 30
        return VideoOutputConfig(
            fps=fps,
            options={"preset": "ultrafast", "crf": "20"}
        )

    def process_image(frame):
        if frame is None:
            return None
        im = frame.to_image()
        mirror = ImageOps.mirror(im)
        new_im = Image.new("RGB", (im.width * 2, im.height))
        new_im.paste(im, (0, 0))
        new_im.paste(mirror, (im.width, 0))
        new_frame = frame.from_image(new_im)
        return new_frame

    process_video(args.input, args.output, config_callback=make_config, frame_callback=process_image)
