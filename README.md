# SSTV Image Transmission Service

This project provides a Python script (`sstv_service.py`) to transmit Slow Scan Television (SSTV) images over a BTECH UV Pro radio TNC via a Bluetooth RFCOMM serial port (`/dev/rfcomm0`) on a Raspberry Pi. The script downloads an image from a URL, encodes it into Martin M1 SSTV audio samples, encapsulates them in AX.25 packets using the KISS protocol, and sends them to the TNC for transmission.

## Functionality
- **Image Download**: Fetches an image from a specified URL using `requests`.
- **SSTV Encoding**: Encodes the image into Martin M1 SSTV audio samples using `pysstv`.
- **AX.25 Framing**: Wraps samples in AX.25 packets with source/destination callsigns and KISS protocol framing.
- **Serial Transmission**: Sends packets to `/dev/rfcomm0` for the BTECH UV Pro TNC.
- **Logging**: Logs to `/var/log/sstv_service.log` with optional debug output for packet details.
- **Systemd Service**: Runs as a systemd service, retransmitting every 300 seconds (configurable) with error handling.

## Requirements

### Python Dependencies
Listed in `requirements.txt`:
```
pysstv==0.5.7
sounddevice==0.5.2
pillow==11.3.0
pyserial==3.5
systemd-python==235
requests==2.32.4
numpy==2.3.2
```
Install:
```bash
cd /home/pi/sstv
pip3 install -r requirements.txt
```
**Note**: If `pysstv==0.3.4` fails on ARM (Raspberry Pi), try:
```bash
pip3 install pysstv
```
Or install from source:
```bash
git clone https://github.com/dnet/pysstv.git
cd pysstv
git checkout v0.3.4
python3 setup.py install
```

### Raspberry Pi `apt` Packages
- Core Python tools: `python3`, `python3-pip`, `python3-dev`, `build-essential`
- Bluetooth support: `bluez`
- Optional monitoring tools: `socat`, `minicom`, `strace`
Install:
```bash
sudo apt-get update
sudo apt-get install python3 python3-pip python3-dev build-essential bluez socat minicom strace
```

## Deployment Steps
1. **Update System**:
   ```bash
   sudo apt-get update
   sudo apt-get upgrade
   ```

2. **Copy Files**:
   Place `sstv_service.py`, `requirements.txt`, and `sstv.service` in `/home/pi/sstv/`:
   ```bash
   mkdir /home/pi/sstv
   scp sstv_service.py requirements.txt sstv.service pi@raspberrypi:/home/pi/sstv/
   ```

3. **Install Dependencies**:
   ```bash
   cd /home/pi/sstv
   pip3 install -r requirements.txt
   ```

4. **Configure Bluetooth RFCOMM**:
   Identify the TNC’s Bluetooth address:
   ```bash
   hcitool scan
   ```
   Bind to `/dev/rfcomm0`:
   ```bash
   sudo rfcomm bind 0 <TNC_BLUETOOTH_ADDRESS> 1
   ```
   Automate binding in `/etc/rc.local`:
   ```bash
   sudo nano /etc/rc.local
   ```
   Add before `exit 0`:
   ```bash
   rfcomm bind 0 <TNC_BLUETOOTH_ADDRESS> 1
   ```

5. **Deploy Systemd Service**:
   Update `sstv.service` with your callsign and image URL:
   ```
   ExecStart=/usr/bin/python3 /home/pi/sstv/sstv_service.py --url https://your-image-service.com/image.jpg --port /dev/rfcomm0 --source-callsign YOURCALL --dest-callsign CQ --interval 300
   ```
   Deploy:
   ```bash
   sudo mv /home/pi/sstv/sstv.service /etc/systemd/system/
   chmod +x /home/pi/sstv/sstv_service.py
   sudo chmod 644 /etc/systemd/system/sstv.service
   sudo touch /var/log/sstv_service.log
   sudo chown pi:pi /var/log/sstv_service.log
   sudo chmod 664 /var/log/sstv_service.log
   sudo systemctl daemon-reload
   sudo systemctl enable sstv.service
   sudo systemctl start sstv.service
   ```

6. **Test Manually**:
   ```bash
   python3 /home/pi/sstv/sstv_service.py --url https://your-image-service.com/image.jpg --port /dev/rfcomm0 --source-callsign YOURCALL --dest-callsign CQ --interval 300
   ```

## Monitoring
- **Logs**:
   ```bash
   tail -f /var/log/sstv_service.log
   ```
   Enable debug logging in `sstv_service.py`:
   ```python
   logging.basicConfig(level=logging.DEBUG, ...)
   ```

- **Serial Data**:
   Stop the service:
   ```bash
   sudo systemctl stop sstv.service
   ```
   Capture data:
   ```bash
   sudo socat -u /dev/rfcomm0,raw,echo=0 - | xxd > /home/pi/rfcomm0_dump.bin
   ```
   Or use `strace`:
   ```bash
   sudo strace -p $(pidof python3) -e trace=write -e write
   ```

## Notes
- **Callsigns**: Replace `YOURCALL` with your amateur radio callsign (e.g., `W1ABC`). Use `CQ` for broadcast.
- **Image URL**: Use the actual URL for your image service.
- **Baud Rate**: Default is 9600; adjust in `sstv_service.py` if needed.
- **Troubleshooting**:
  - Verify `/dev/rfcomm0`:
    ```bash
    ls /dev/rfcomm*
    ```
  - Ensure TNC accepts KISS-framed AX.25 packets.
  - Use an SSTV receiver to verify transmission.