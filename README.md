# TikTok Rare Username Checker

## Overview
A simple tool to check the availability of TikTok usernames.

## About
This proxyless tool checks the availability of TikTok usernames at high speed. It determines availability based on the HTTP request status code. If the status code is not `200`, the tool assumes the username is available.

**Note:** This tool can be inaccurate as it does not use the official TikTok API. It only checks if the username's TikTok URL is invalid.

**Disclaimer:** This tool is for educational purposes only.

## Requirements
- Python 3.7.6 (not compatible with Python 3.9.13 or newer)

## Installation
1. Download and install Python 3.7.6. Ensure you select the 'ADD TO PATH' option during installation.
2. Install the required package by running:
   ```sh
   pip install aiohttp
   ```

## Usage
1. Add the usernames you want to check in `usernames.txt`. To avoid rate limiting, it is recommended to check around 500 usernames and then wait 24 hours before checking again.
2. Ensure you are in the same directory as the downloaded folder. Run the tool by typing:
   ```sh
   python main.py
   ```
3. Once the tool has finished running, available usernames will be saved in `hits.txt`.

## Limitations
- This tool does not work with Python 3.9.13 or newer.
- It is proxyless and may result in rate limiting if too many usernames are checked at once.
