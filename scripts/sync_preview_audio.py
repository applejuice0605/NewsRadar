#!/usr/bin/env python3
"""Overlay isolated trial episodes without rolling the preview's latest date back."""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil


def latest_day(path):
    try:
        value=json.loads(path.read_text());day=date.fromisoformat(value['date'])
        if day.isoformat()!=value['date']: return None
        return day
    except (OSError,ValueError,KeyError,TypeError): return None


def sync(source,destination):
    source,destination=Path(source),Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    for child in source.iterdir():
        if not child.is_dir() or child.is_symlink(): continue
        try:
            if date.fromisoformat(child.name).isoformat()!=child.name: continue
        except ValueError: continue
        shutil.copytree(child,destination/child.name,dirs_exist_ok=True)
    incoming=latest_day(source/'latest.json');current=latest_day(destination/'latest.json')
    if incoming is not None and (current is None or incoming>=current):
        shutil.copyfile(source/'latest.json',destination/'latest.json')
        return True
    return False


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True);args=parser.parse_args()
    print('[preview-audio] latest advanced='+str(sync(args.source,args.destination)))
