"""Read-only semantic search inside an explicitly requested mining sandbox."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['search-learnings'])
    parser.add_argument('query')
    parser.add_argument('--top',type=int,default=8)
    args=parser.parse_args(argv)
    from .config import Config
    from .search import search_corpus
    context=json.loads(Path('search-corpus.json').read_text(encoding='utf-8'))
    result=search_corpus(Config(**context['configuration']),context['corpus'],args.query,top_k=args.top,with_meta=True)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
