import json

import pytest

from scripts.summarize_segmentation import characters, comparison, edit_distance, sentence_errors, words


@pytest.mark.parametrize('reference,hypothesis,expected', [
    ('abc', 'axc', 1), ('abc', '', 3), ('', 'abc', 3),
    (['turnips', 'and', 'carrots'], ['turnips', 'carrots'], 1),
])
def test_reference_edit_counts_cover_substitution_deletion_and_insertion(reference, hypothesis, expected):
    assert edit_distance(reference, hypothesis) == expected


def test_sentence_scoring_preserves_overlap_duplicates_and_counts_missing_output():
    records = [{'sample_id': 'english', 'iteration': 1}, {'sample_id': 'english', 'iteration': 2}]
    segments = [{'sample_id': 'english', 'iteration': 1, 'asr_text': 'turnips and'},
                {'sample_id': 'english', 'iteration': 1, 'asr_text': 'and carrots'}]
    result = sentence_errors(records, segments, 'english', 'turnips and carrots', words)
    # One duplicated 'and' in round 1 plus three missing words in round 2.
    assert result['edits'] == 4
    assert result['reference_units'] == 6
    assert result['percent'] == 66.667
    assert sentence_errors([], [], 'absent', 'turnips', words)['percent'] is None


def test_normalization_removes_punctuation_but_keeps_foreign_hallucinations():
    assert characters('Ａ、落ち着き。 감사합니다.') == 'a落ち着き감사합니다'
    assert words('FLOUR-FATTENED sauce.') == ['flour', 'fattened', 'sauce']


def test_unmapped_outputs_cannot_be_scored_as_per_sample_accuracy(tmp_path):
    report = tmp_path / 'report.json'
    report.write_text('{}')
    quality = tmp_path / 'quality.json'
    quality.write_text(json.dumps({'sample_mapping': 'unavailable'}))
    checkpoint = tmp_path / 'checkpoint.json'
    checkpoint.write_text(json.dumps({'cells': [{'status': 'complete', 'report_path': str(report),
                                                'quality_path': str(quality)}]}))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'sources': {'japanese': {'reference': 'あ'}}}))
    with pytest.raises(ValueError, match='verified sample mapping'):
        comparison(checkpoint, manifest)
