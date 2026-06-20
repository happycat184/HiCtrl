# HiCtrl

HiCtrl is a supervised remote assistance prototype written in Python. It requires an explicit acceptance prompt on the assisted machine before screen viewing and input control begin.

## Features

- Custom framed protocol:
  - `0x0F 0x0A`
  - 2-byte package id
  - 8-byte payload length
  - payload
  - `0x0A 0x0F`
- Staged session flow:
  - `Handshake -> Connect -> Encrypt -> Control -> Close`
- RSA-OAEP key exchange for session setup
- AES-GCM encryption for the `Control` stage only
- Screen streaming with selectable resolution presets up to native desktop size
- Mouse and keyboard forwarding
- Unicode text injection for Chinese and other non-ASCII text
- Visible session banner and stop button on the assisted machine

## Install

```powershell
python -m pip install -r requirements.txt
```

## Run

Start the assisted side:

```powershell
python src\agent.py
```

Start the controller side:

```powershell
python src\controller.py
```

## Usage

- Use the `Stream` drop-down on the controller to switch between `1280x720`, `1600x900`, `1920x1080`, `2560x1440`, and `Native`. Each preset also adjusts JPEG quality and target FPS for lower end-to-end latency.
- Use the `Send Text` box on the controller to input Chinese text and press `Send` or `Enter`.

## Notes

- This prototype is intended for visible, supervised support sessions only.
- There is no persistence, stealth mode, or unattended access.
- The agent window shows an active-session banner while control is enabled.
