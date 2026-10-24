"""Summarize matrix reports and narrow, independently referenced sentence errors."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from scripts.latency_benchmark import distribution, summarize


def characters(text):
    return ''.join(character for character in unicodedata.normalize('NFKC', text).casefold()
                   if not character.isspace() and not unicodedata.category(character).startswith('P'))


def words(text):
    return re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold())


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, start=1):
        current = [index]
        for column, actual in enumerate(hypothesis, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def sentence_errors(records, segments, sample_id, reference, tokenizer):
    """Count all output, including duplicates and foreign-language hallucinations."""
    expected = tokenizer(reference)
    if not expected:
        raise ValueError('A sentence reference must contain scored characters or words.')
    iterations = sorted({record['iteration'] for record in records if record['sample_id'] == sample_id})
    rounds = []
    for iteration in iterations:
        text = ' '.join(segment.get('asr_text', '') or '' for segment in segments
                        if segment.get('sample_id') == sample_id and segment.get('iteration') == iteration)
        actual = tokenizer(text)
        rounds.append({'iteration': iteration, 'edits': edit_distance(expected, actual),
                       'reference_units': len(expected), 'hypothesis_units': len(actual)})
    denominator = len(expected) * len(rounds)
    errors = sum(row['edits'] for row in rounds)
    return {'edits': errors, 'reference_units': denominator,
            'percent': round(100 * errors / denominator, 3) if denominator else None,
            'rounds': rounds}


def _report_path(checkpoint, value):
    path = Path(value)
    return path if path.exists() else checkpoint.parent / path.name


def comparison(checkpoint_path, manifest_path, english_reference=None):
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    japanese_reference = manifest['sources']['japanese']['reference']
    cells = []
    for cell in checkpoint['cells']:
        if cell['status'] not in ('complete', 'HOLD'):
            continue
        report = json.loads(_report_path(checkpoint_path, cell['report_path']).read_text(encoding='utf-8'))
        quality_path = _report_path(checkpoint_path, cell['quality_path'])
        quality = json.loads(quality_path.read_text(encoding='utf-8'))
        if quality['sample_mapping'] != 'verified-session-ordinal':
            raise ValueError('Quality evidence cannot be scored without a verified sample mapping.')
        per_round = {}
        for iteration in range(1, report['repeats'] + 1):
            records = [record for record in report['records'] if record['iteration'] == iteration]
            per_round[str(iteration)] = summarize(records)
            per_round[str(iteration)]['playback_to_first_readable_ms'] = distribution(
                record.get('playback_to_first_readable_ms') for record in records)
        cells.append({
            'name': cell['name'], 'configuration': cell['configuration'],
            'summary': report['summary'], 'per_sample': report['per_sample'], 'per_round': per_round,
            'first_caption_per_session': report.get('first_caption_per_session'),
            'gpu': report['gpu'], 'sessions': len(report['records']),
            'measurement_complete': report['measurement_complete'],
            'quality_mapping': quality['sample_mapping'],
            'quality_sidecar_sha256': hashlib.sha256(quality_path.read_bytes()).hexdigest(),
            'effective_modes': quality['effective_endpoint_modes'],
            'japanese_sentence_cer': sentence_errors(report['records'], quality['segments'],
                                                      'japanese', japanese_reference, characters),
            'english_sentence_wer': sentence_errors(report['records'], quality['segments'],
                                                     'english', english_reference, words)
                                    if english_reference is not None else None,
        })
    return {
        'schema_version': 1, 'checkpoint_status': checkpoint['status'],
        'completed_cells': checkpoint['completed_cells'], 'total_cells': checkpoint['total_cells'],
        'definitions': {
            'scope': 'One independently referenced Japanese sentence and, optionally, one English sentence. This does not establish broad accuracy or a release gate.',
            'japanese': 'NFKC, casefold, remove punctuation and whitespace, character Levenshtein distance.',
            'english': 'NFKC, casefold, Unicode letter/number word tokens (hyphens separate words), word Levenshtein distance.',
            'aggregation': 'Concatenate all ASR text for each sample/round without deduplication; sum edits and reference lengths across rounds. A present session with no output counts as deletion of the entire reference.',
            'privacy': 'Only counts and reference hashes are exported; local dialogue sidecars are not copied.',
        },
        'references': {
            'japanese_text_sha256': hashlib.sha256(japanese_reference.encode('utf-8')).hexdigest(),
            'english_text_sha256': hashlib.sha256(english_reference.encode('utf-8')).hexdigest()
                                   if english_reference is not None else None,
        },
        'cells': cells,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoint', type=Path)
    parser.add_argument('--manifest', type=Path, default=Path('benchmarks/baseline.json'))
    parser.add_argument('--english-reference', type=Path, help='Local dataset annotation; see benchmarks/quality-references.json.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = args.english_reference.read_text(encoding='utf-8').strip() if args.english_reference else None
    result = comparison(args.checkpoint, args.manifest, reference)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(args.output)
