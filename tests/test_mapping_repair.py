import copy
import unittest
from unittest.mock import AsyncMock

from ai_video.video_higgsfield_jutsu.python_backend.spatial_guides import SpatialGuideBuilder, GuideError


def candidate(confidence=0.4):
    return {'cameraSummary': 'High angle sea view.', 'entities': [
        {'id': 'sea', 'label': 'Sea background', 'appearance': 'Open sea.',
         'assetIndices': [1], 'sourceObjectIds': [], 'confidence': confidence,
         'frames': [{'index': 0, 'visible': True, 'bbox': [0, 0, 1, 1]}]},
    ]}


class MappingRepairTests(unittest.IsolatedAsyncioTestCase):
    async def repair(self, responses, bundle=None):
        builder = SpatialGuideBuilder(None, None)
        builder.analyze = AsyncMock(side_effect=responses)
        bundle = bundle if bundle is not None else {}
        snapshots = []
        await builder.repair_mapping(bundle, 'Map the assets.', [], [{'role': 'background'}],
                                     [{'index': 0}], None,
                                     lambda: snapshots.append(copy.deepcopy(bundle)), None)
        return builder, bundle, snapshots

    async def test_sends_error_and_candidate_back_to_ai(self):
        builder, bundle, snapshots = await self.repair([candidate(), candidate(0.9)])
        self.assertEqual(builder.analyze.await_count, 2)
        self.assertIn('Sea background', builder.analyze.call_args.args[2])
        self.assertIn('previousCandidate', builder.analyze.call_args.args[2])
        self.assertEqual(bundle['mapping']['entities'][0]['confidence'], 0.9)
        self.assertEqual(len(bundle['mappingAttempts']), 2)
        self.assertIn('candidate', snapshots[0]['mappingAttempts'][0])

    async def test_low_confidence_continues_with_warning_after_three_attempts(self):
        builder, bundle, _ = await self.repair([candidate(), candidate(0.5), candidate(0.3)])
        self.assertEqual(builder.analyze.await_count, 3)
        self.assertTrue(bundle['mappingWarnings'])
        self.assertEqual(bundle['mapping']['entities'][0]['confidence'], 0.5)

    async def test_resume_reuses_persisted_candidates_without_exceeding_budget(self):
        bundle = {'mappingAttempts': [
            {'attempt': i + 1, 'candidate': candidate(), 'issue': 'low confidence'} for i in range(3)
        ]}
        builder, bundle, _ = await self.repair([], bundle)
        self.assertEqual(builder.analyze.await_count, 0)
        self.assertIn('mapping', bundle)

    async def test_structural_errors_are_repaired_and_never_silently_accepted(self):
        invalid = candidate(0.9)
        invalid['entities'][0]['assetIndices'] = [2]
        builder, bundle, _ = await self.repair([invalid, candidate(0.9)])
        self.assertEqual(builder.analyze.await_count, 2)
        self.assertEqual(bundle['mapping']['entities'][0]['assetIndices'], [1])
        with self.assertRaises(GuideError):
            await self.repair([invalid] * 5)

    async def test_missing_required_field_triggers_fresh_regeneration_after_repairs(self):
        invalid = candidate(0.9)
        del invalid['entities'][0]['label']
        builder, bundle, _ = await self.repair([invalid] * 3 + [candidate(0.9)])
        self.assertEqual(builder.analyze.await_count, 4)
        self.assertEqual(bundle['mappingAttempts'][3]['mode'], 'regenerate')
        self.assertIn('$.entities[0].label', builder.analyze.call_args.args[2])
        self.assertIn('requiredSchema', builder.analyze.call_args.args[2])
        self.assertEqual(bundle['mapping']['entities'][0]['label'], 'Sea background')

    async def test_resume_exhausted_repairs_can_regenerate_without_replaying_three_calls(self):
        invalid = candidate(0.9)
        del invalid['entities'][0]['frames'][0]['visible']
        bundle = {'mappingAttempts': [
            {'attempt': i + 1, 'candidate': invalid, 'issue': 'missing visible'} for i in range(3)
        ]}
        builder, bundle, _ = await self.repair([candidate(0.9)], bundle)
        self.assertEqual(builder.analyze.await_count, 1)
        self.assertEqual(len(bundle['mappingAttempts']), 4)

    async def test_five_structural_failures_are_bounded_and_report_the_last_cause(self):
        invalid = candidate(0.9)
        del invalid['entities'][0]['appearance']
        builder = SpatialGuideBuilder(None, None)
        builder.analyze = AsyncMock(side_effect=[invalid] * 5)
        bundle = {}
        with self.assertRaisesRegex(GuideError, r'5회.*appearance'):
            await builder.repair_mapping(bundle, 'Map.', [], [{'role': 'background'}],
                                         [{'index': 0}], None, lambda: None, None)
        self.assertEqual(builder.analyze.await_count, 5)
        self.assertEqual(bundle['mappingStatus'], 'unresolved')
        with self.assertRaises(GuideError):
            await builder.repair_mapping(bundle, 'Map.', [], [{'role': 'background'}],
                                         [{'index': 0}], None, lambda: None, None)
        self.assertEqual(builder.analyze.await_count, 5)

    async def test_offscreen_character_keeps_a_complete_hidden_frame_sample(self):
        hidden = candidate(0.9)
        hidden['entities'][0]['frames'][0].update(visible=False, bbox=[0, 0, 0, 0])
        builder, bundle, _ = await self.repair([hidden])
        self.assertEqual(builder.analyze.await_count, 1)
        self.assertFalse(bundle['mapping']['entities'][0]['frames'][0]['visible'])


    async def test_mapping_schema_constrains_current_asset_numbers(self):
        builder, _, _ = await self.repair([candidate(0.9)])
        schema = builder.analyze.call_args.args[1]
        indices = schema['properties']['entities']['items']['properties']['assetIndices']
        self.assertEqual(indices['minItems'], 1)
        self.assertEqual(indices['items']['enum'], [1])

    async def test_empty_and_out_of_range_numbers_have_targeted_feedback(self):
        for indices in ([], [0], [2]):
            with self.subTest(indices=indices):
                invalid = candidate(0.9)
                invalid['entities'][0]['assetIndices'] = indices
                builder, bundle, _ = await self.repair([invalid, candidate(0.9)])
                issue = bundle['mappingAttempts'][0]['issue']
                self.assertIn('$.entities[0].assetIndices', issue)
                self.assertIn('Sea background', issue)
                self.assertIn('allowedAssetIndices', issue)
                self.assertIn('receivedAssetIndices', issue)
                self.assertIn(issue, builder.analyze.call_args.args[2].replace('\\"', '"'))

    async def test_original_sheet_number_is_not_the_mapping_number(self):
        builder = SpatialGuideBuilder(None, None)
        mapped = candidate(0.9)
        other = copy.deepcopy(mapped['entities'][0])
        other.update(id='second', label='Second member', assetIndices=[2])
        mapped['entities'].append(other)
        builder.analyze = AsyncMock(return_value=mapped)
        assets = [{'role': 'character', 'sourceIndex': 1, 'label': label}
                  for label in ('Member A', 'Member B')]
        await builder.repair_mapping({}, 'Map.', [], assets, [{'index': 0}], None,
                                     lambda: None, None)
        call = builder.analyze.call_args
        indices = call.args[1]['properties']['entities']['items']['properties']['assetIndices']
        self.assertEqual(indices['items']['enum'], [1, 2])
        self.assertIn('sourceIndex is provenance only', call.args[2])
        self.assertIn('layout and timeline image numbers', call.args[2])
