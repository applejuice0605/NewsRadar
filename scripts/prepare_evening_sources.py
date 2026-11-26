"""Freeze source bodies before22:00 for the final shared factual edition."""
from pathlib import Path
import sys

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'TrendRadar'))
sys.path.insert(0,str(REPO/'scripts'))
from generate_daily_podcast import load_podcast_config
from trendradar.ai.daily_edition import capture_sources

if __name__=='__main__':
    config,_=load_podcast_config()
    path=capture_sources(REPO,settings=config.get('materials'))
    print('[edition] pre-cutoff source bodies captured:',path)
