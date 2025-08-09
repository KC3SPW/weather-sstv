#!/usr/bin/env python3

import time
import serial
import numpy as np
from pysstv.sstv import SSTV
from PIL import Image
import sounddevice as sd
from systemd import journal
import logging
import argparse
import requests
import tempfile
import os

# Constants from pysstv
FREQ_VIS_BIT1 = 1100
FREQ_SYNC = 1200
FREQ_VIS_BIT0 = 1300
FREQ_BLACK = 1500
FREQ_VIS_START = 1900
FREQ_FSKID_BIT1 = 1900
FREQ_FSKID_BIT0 = 2100
FREQ_WHITE = 2300
FREQ_RANGE = FREQ_WHITE - FREQ_BLACK
MSEC_VIS_START = 300
MSEC_VIS_SYNC = 10
MSEC_VIS_BIT = 30
MSEC_FSKID_BIT = 22


# Define byte_to_freq function
def byte_to_freq(value):
    """Convert pixel intensity (0-255) to frequency (1500-2300 Hz)."""
    return FREQ_BLACK + FREQ_RANGE * value / 255


# Configure logging to systemd journal
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('SSTVService')
logger.handlers = []  # Clear default handlers
logger.addHandler(journal.JournalHandler())


class MartinM1(SSTV):
    VIS_CODE = 44  # Martin M1 VIS code
    SYNC = 4.862
    WIDTH = 320
    HEIGHT = 256

    def __init__(self, image, samples_per_sec, bits):
        super().__init__(image, samples_per_sec, bits)
        self.image = image.convert('RGB').resize((self.WIDTH, self.HEIGHT))

    def gen_image_tuples(self):
        # Horizontal sync pulse
        yield from self.horizontal_sync()
        # Separator pulse
        yield 1462, 0.5

        for y in range(self.HEIGHT):
            # Green scan line
            for x in range(self.WIDTH):
                r, g, b = self.image.getpixel((x, y))
                yield byte_to_freq(g), 0.146  # 146 us per pixel
            yield from self.horizontal_sync()
            yield 1462, 0.5

            # Blue scan line
            for x in range(self.WIDTH):
                r, g, b = self.image.getpixel((x, y))
                yield byte_to_freq(b), 0.146
            yield from self.horizontal_sync()
            yield 1462, 0.5

            # Red scan line
            for x in range(self.WIDTH):
                r, g, b = self.image.getpixel((x, y))
                yield byte_to_freq(r), 0.146
            yield from self.horizontal_sync()
            yield 1462, 0.5


def download_image(url, timeout=10, retries=3, retry_delay=60):
    """Download an image from a URL and return a PIL Image object."""
    for attempt in range(retries):
        try:
            logger.info(f"Attempting to download image from {url} (Attempt {attempt + 1}/{retries})")
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()  # Raise an exception for bad status codes
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
                tmp_file.write(response.content)
                tmp_file_path = tmp_file.name
            try:
                img = Image.open(tmp_file_path)
                img.verify()  # Verify image integrity
                img = Image.open(tmp_file_path)  # Reopen after verify
                return img, tmp_file_path
            finally:
                os.unlink(tmp_file_path)  # Clean up temporary file
        except (requests.RequestException, Image.UnidentifiedImageError) as e:
            logger.error(f"Failed to download or process image: {e}")
            if attempt < retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                raise
    raise Exception(f"Failed to download image after {retries} attempts")


def encode_sstv_image(url, samples_per_sec=44100, bits=16):
    try:
        img, tmp_file_path = download_image(url)
        try:
            sstv = MartinM1(img, samples_per_sec, bits)
            sstv.vox_enabled = True
            sstv.add_fskid_text("RPI_SSTV")
            samples = np.array(list(sstv.gen_samples()), dtype=np.int16 if bits == 16 else np.int8)
            return samples, samples_per_sec
        finally:
            img.close()
    except Exception as e:
        logger.error(f"Error encoding SSTV image from {url}: {e}")
        raise


def transmit_sstv(samples, samplerate, serial_port, baudrate=9600):
    try:
        with serial.Serial(serial_port, baudrate, timeout=1) as ser:
            # Configure audio output to serial port
            sd.default.device = None  # Use default audio device
            sd.default.samplerate = samplerate
            sd.default.channels = 1
            sd.default.dtype = 'int16' if samples.dtype == np.int16 else 'int8'

            # Play audio samples
            logger.info("Starting SSTV transmission")
            sd.play(samples, samplerate=samplerate, blocking=True)
            sd.wait()  # Wait for playback to complete
            logger.info("SSTV transmission completed")
    except Exception as e:
        logger.error(f"Error during SSTV transmission: {e}")
        raise


def sstv_service(url, serial_port, interval=300):
    logger.info("Starting SSTV service")
    while True:
        try:
            samples, samplerate = encode_sstv_image(url)
            transmit_sstv(samples, samplerate, serial_port)
            logger.info(f"Waiting {interval} seconds before next transmission")
            time.sleep(interval)
        except Exception as e:
            logger.error(f"Service error: {e}")
            time.sleep(60)  # Wait before retrying on error


def main():
    parser = argparse.ArgumentParser(description="SSTV Image Transmission Service")
    parser.add_argument('--url', default='https://example.com/image.jpg', help='URL of the image to transmit')
    parser.add_argument('--port', default='/dev/ttyUSB0', help='Serial port for KISS TNC')
    parser.add_argument('--interval', type=int, default=300, help='Interval between transmissions in seconds')
    args = parser.parse_args()

    sstv_service(args.url, args.port, args.interval)


if __name__ == "__main__":
    main()
