# TC Follower Scanner

Find every Instagram account with 100k+ followers that follows @trueclassic.

## What it does
- Paginates through all followers of any Instagram account via RocketAPI
- Filters to accounts above your minimum follower threshold (default: 100k)
- Shows results live as they're found
- One-click CSV download when complete

## Stack
- Python / Flask
- RocketAPI (Instagram private API wrapper)
- Deployed on Render

## Setup
1. Get a RocketAPI key at https://rocketapi.io (7-day free trial)
2. Set env var `ROCKETAPI_KEY` on Render (optional — can also enter in UI)
3. Deploy to Render as a Python web service

## Environment Variables
- `ROCKETAPI_KEY` — your RocketAPI token (optional, can enter in UI)
- `MIN_FOLLOWERS` — default minimum follower threshold (default: 100000)
- `PORT` — set automatically by Render
