"""Synthetic, reproducible local benchmark; no model API or real documents."""
import argparse
import json
import pathlib
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'python'))
from team_drive.engine import Engine

def measured(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', type=int, default=200)
    ap.add_argument('--rows', type=int, default=10000)
    args = ap.parse_args()
    if not 1 <= args.files <= 10000 or not 1 <= args.rows <= 90000:
        ap.error('Use 1..10000 files and 1..90000 rows.')
    with tempfile.TemporaryDirectory(prefix='team-drive-bench-') as temp:
        work = pathlib.Path(temp)
        source = work / 'source'
        source.mkdir()
        for index in range(args.files):
            (source / ('document-%05d.txt' % index)).write_text('SIMULATED 项目预算 基准资料 item-%05d\n' % index, encoding='utf-8')
        with (source / 'large.csv').open('w', encoding='utf-8') as output:
            output.write('id,currency,amount\n')
            for index in range(args.rows):
                output.write('%d,CNY,123.45\n' % index)
        config = work / 'config.json'
        config.write_text(json.dumps({'provider':'local','root':'source','state_dir':'state','poll_seconds':3600}), encoding='utf-8')
        engine = Engine(str(config), autostart=False)
        try:
            _, cold = measured(engine.scan_once)
            coverage = engine.status()['coverage']
            assert coverage['all_present_files_indexed'], coverage
            times = [measured(lambda: engine.search('预算'))[1] for _ in range(30)]
            large = engine.list(prefix='large.csv')['files'][0]
            assert large['segments_count'] == args.rows + 1
            data, read_ms = measured(lambda: engine.read(large['file_id'], start=args.rows, limit=1))
            assert data['segments'][0]['data']['cells'][0]['value'] == str(args.rows - 1)
            artifact, export_ms = measured(lambda: engine.export(large['file_id'], large['source_version']))
            assert artifact['row_count'] == args.rows + 1
            (source / 'document-00000.txt').write_text('SIMULATED 项目预算 changed\n', encoding='utf-8')
            _, increment = measured(engine.scan_once)
            result = {'corpus':'synthetic','platform':platform.system(),'python':platform.python_version(),
                      'small_files':args.files,'csv_data_rows':args.rows,'cold_index_ms':round(cold,2),
                      'warm_keyword_search_p50_ms':round(statistics.median(times),2),
                      'warm_keyword_search_p95_ms':round(sorted(times)[28],2),
                      'tail_row_read_with_source_hash_ms':round(read_ms,2),
                      'full_jsonl_export_ms':round(export_ms,2),'single_changed_file_reconcile_ms':round(increment,2),
                      'coverage':coverage,'note':'Local backend timings on generated data; not production latency guarantees or measured peak memory.'}
            output = pathlib.Path(__file__).resolve().parents[1] / '.local/benchmark.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:
            engine.close()

if __name__ == '__main__':
    main()
