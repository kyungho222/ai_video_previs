"""Concise, non-blocking diagnostics for generated speech and audio."""
import re

_AUDIO = re.compile(r'음성|목소리|대사|발화|발음|한국어|영어|음악|노래|노래방|효과음|말한|말하|외치|랩|목청|voice|speech|speak|spoken|dialogue|audio|vocal|sing|singing|music|lyrics|sound|korean', re.I)


def _audio_fragments(value):
    if not isinstance(value, str):
        return []
    chunks = re.split(r'(?<=[.!?])\s+|\n+', value)
    return [part.strip() for part in chunks if part.strip() and (_AUDIO.search(part) or re.search(r'[\"“][^\"”]+[\"”]', part))]


def build_voice_diagnostics(job_id, options, direction, final_prompt):
    plan = direction.get('plan') if isinstance(direction, dict) else None
    plan = plan if isinstance(plan, dict) else {}
    profiles = plan.get('characters') if isinstance(plan.get('characters'), list) else []
    dialogues = plan.get('dialogues') if isinstance(plan.get('dialogues'), list) else []
    return {
        'jobId': job_id,
        'provider': options.get('provider'),
        'model': options.get('model'),
        'audioMode': options.get('audioMode') or 'unknown',
        'audioAssetCount': len(options.get('audioAssets') or []),
        'directionStatus': direction.get('status') if isinstance(direction, dict) else None,
        'userAudioInstructions': _audio_fragments(options.get('prompt', '')),
        'scenarioAudioInstructions': _audio_fragments(options.get('scenePrompt', '')),
        'voiceProfiles': [{k: p.get(k) for k in ('character_id', 'voice_language', 'voice_description')}
                          for p in profiles if isinstance(p, dict)],
        'dialogues': [{k: d.get(k) for k in ('dialogue_id', 'speaker_ids', 'text', 'delivery', 'start', 'end')}
                      for d in dialogues if isinstance(d, dict)],
        'submittedAudioInstructions': _audio_fragments(final_prompt),
        'fullSubmittedPromptLocation': 'metadata.submittedPrompt',
    }
