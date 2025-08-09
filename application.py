#!/usr/bin/env python3

import sys
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
import struct

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

# AX.25 and KISS constants
KISS_FEND = 0xC0  # Frame End
KISS_FTYPE_DATA = 0x00  # Data frame type
AX25_CONTROL = 0x03  # UI frame (Unnumbered Information)
AX25_PID = 0xF0  # No layer 3 protocol
AX25_MAX_PAYLOAD = 256  # Max payload size per packet

# Define byte_to_freq function
def byte_to_freq(value):
    """Convert pixel intensity (0-255) to frequency (1500-2300 Hz)."""
    return FREQ_BLACK + FREQ_RANGE * value / 255

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

def encode_ax25_packet(source_callsign, dest_callsign, payload):
    """Create an AX.25 packet with the given payload."""
    # Convert callsigns to AX.25 address format (7 bytes each: 6 chars + SSID)
    def callsign_to_ax25(callsign, is_last=False):
        # Pad callsign to 6 characters, uppercase
        callsign = callsign.upper().ljust(6, ' ')
        # Convert to bytes, shift left by 1 bit (AX.25 requirement)
        addr = bytes(ord(c) << 1 for c in callsign)
        # SSID byte: 0 for simplicity, set bit 0 to 1 for last address
        ssid = 0x60 | (0x01 if is_last else 0x00)
        return addr + bytes([ssid])

    # Build AX.25 frame
    dest_addr = callsign_to_ax25(dest_callsign)
    src_addr = callsign_to_ax25(source_callsign, is_last=True)
    frame = dest_addr + src_addr + bytes([AX25_CONTROL, AX25_PID]) + payload

    # Build KISS frame
    kiss_frame = bytes([KISS_FEND, KISS_FTYPE_DATA]) + frame + bytes([KISS_FEND])
    # Escape FEND (0xC0) and FESC (0xDB) in the frame (excluding delimiters)
    kiss_frame = kiss_frame[0:2] + kiss_frame[2:-1].replace(b'\xC0', b'\xDB\xDC').replace(b'\xDB', b'\xDB\xDD') + kiss_frame[-1:]
    return kiss_frame

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

def transmit_sstv(samples, samplerate, serial_port, source_callsign="N0CALL", dest_callsign="CQ", baudrate=9600):
    try:
        with serial.Serial(serial_port, baudrate, timeout=1) as ser:
            # Log sample information
            logger.info(f"Encoding {len(samples)} audio samples into AX.25 packets for {serial_port} at {samplerate} Hz")
            # Log a subset of samples for debugging (first 100 samples)
            logger.debug(f"Sample data (first 100): {samples[:100].tolist()}")

            # Convert samples to bytes
            sample_bytes = samples.tobytes()
            logger.info(f"Total payload size: {len(sample_bytes)} bytes")

            # Split into AX.25 packets
            chunk_size = AX25_MAX_PAYLOAD
            for i in range(0, len(sample_bytes), chunk_size):
                chunk = sample_bytes[i:i + chunk_size]
                packet = encode_ax25_packet(source_callsign, dest_callsign, chunk)
                logger.debug(f"Sending AX.25 packet with {len(chunk)} bytes payload (total packet size: {len(packet)})")
                ser.write(packet)
                ser.flush()
                time.sleep(0.02)  # Delay to prevent TNC buffer overflow
                logger.debug(f"Wrote AX.25 packet to {serial_port}")

            logger.info("SSTV transmission completed")
    except Exception as e:
        logger.error(f"Error during SSTV transmission: {e}")
        raise

def sstv_service(url, serial_port, source_callsign="N0CALL", dest_callsign="CQ", interval=300):
    logger.info("Starting SSTV service")
    while True:
        try:
            samples, samplerate = encode_sstv_image(url)
            transmit_sstv(samples, samplerate, serial_port, source_callsign, dest_callsign)
            logger.info(f"Waiting {interval} seconds before next transmission")
            time.sleep(interval)
        except Exception as e:
            logger.error(f"Service error: {e}")
            time.sleep(60)  # Wait before retrying on error

def main():
    parser = argparse.ArgumentParser(description="SSTV Image Transmission Service")
    parser.add_argument('--url', default='https://example.com/image.jpg', help='URL of the image to transmit')
    parser.add_argument('--port', default='/dev/rfcomm0', help='Serial port for KISS TNC')
    parser.add_argument('--source-callsign', default='N0CALL', help='Source callsign for AX.25 packets')
    parser.add_argument('--dest-callsign', default='CQ', help='Destination callsign for AX.25 packets')
    parser.add_argument('--interval', type=int, default=300, help='Interval between transmissions in seconds')
    args = parser.parse_args()

    sstv_service(args.url, args.port, args.source_callsign, args.dest_callsign, args.interval)

if __name__ == "__main__":
    main()