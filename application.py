#!/usr/bin/env python3

import time
import serial
import numpy as np
from pysstv.sstv import SSTV
from PIL import Image
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

# KISS protocol constants
KISS_FEND = 0xC0  # Frame End
KISS_FESC = 0xDB  # Frame Escape
KISS_TFEND = 0xDC  # Transposed Frame End
KISS_TFESC = 0xDD  # Transposed Frame Escape
KISS_DATA = 0x00  # Data frame type

# Configure logging to file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/sstv_service.log'),
        logging.StreamHandler()  # Also log to console for debugging
    ]
)
logger = logging.getLogger('SSTVService')


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

    def gen_samples(self):
        """Generate raw audio samples for Martin M1 SSTV."""
        samples = []
        phase = 0.0
        for freq, duration in self.gen_tones():
            n_samples = int(duration * self.samples_per_sec / 1000.0)
            for i in range(n_samples):
                phase_inc = 2 * np.pi * freq / self.samples_per_sec
                sample = np.sin(phase)
                phase = (phase + phase_inc) % (2 * np.pi)
                sample_value = int(sample * ((1 << (self.bits - 1)) - 1))
                samples.append(sample_value)
        return samples


def byte_to_freq(value):
    """Convert pixel intensity (0-255) to frequency (1500-2300 Hz)."""
    return FREQ_BLACK + FREQ_RANGE * value / 255


def download_image(url, timeout=10, retries=3, retry_delay=60):
    """Download an image from a URL and return a PIL Image object."""
    for attempt in range(retries):
        try:
            logger.info(f"Attempting to download image from {url} (Attempt {attempt + 1}/{retries})")
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
                tmp_file.write(response.content)
                tmp_file_path = tmp_file.name
            try:
                img = Image.open(tmp_file_path)
                img.verify()
                img = Image.open(tmp_file_path)
                return img, tmp_file_path
            finally:
                os.unlink(tmp_file_path)
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
            logger.info(
                f"Transmitting {len(samples)} raw audio samples to {serial_port} at {samplerate} Hz via KISS TNC")
            logger.debug(f"Sample data (first 100): {samples[:100].tolist()}")

            # Convert samples to bytes
            sample_bytes = samples.tobytes()
            logger.info(f"Total sample size: {len(sample_bytes)} bytes")

            # KISS frame buffer
            chunk_size = 800  # KISS frame data payload size (avoid exceeding typical TNC buffer limits)
            for i in range(0, len(sample_bytes), chunk_size):
                chunk = sample_bytes[i:i + chunk_size]

                # Create KISS frame
                kiss_frame = bytearray()
                kiss_frame.append(KISS_FEND)  # Start of frame
                kiss_frame.append(KISS_DATA)  # Data frame type

                # Escape special bytes in the chunk
                for byte in chunk:
                    if byte == KISS_FEND:
                        kiss_frame.extend([KISS_FESC, KISS_TFEND])
                    elif byte == KISS_FESC:
                        kiss_frame.extend([KISS_FESC, KISS_TFESC])
                    else:
                        kiss_frame.append(byte)

                kiss_frame.append(KISS_FEND)  # End of frame

                # Write KISS frame to serial port
                ser.write(kiss_frame)
                ser.flush()
                logger.debug(f"Wrote KISS frame of {len(kiss_frame)} bytes to {serial_port}")

                # Small delay to prevent overwhelming the TNC
                time.sleep(0.01)

            logger.info("KISS SSTV transmission completed")
    except Exception as e:
        logger.error(f"Error during KISS SSTV transmission: {e}")
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
    parser.add_argument('--port', default='/dev/rfcomm0', help='Serial port for KISS TNC')
    parser.add_argument('--interval', type=int, default=300, help='Interval between transmissions in seconds')
    args = parser.parse_args()

    sstv_service(args.url, args.port, args.interval)


if __name__ == "__main__":
    main()