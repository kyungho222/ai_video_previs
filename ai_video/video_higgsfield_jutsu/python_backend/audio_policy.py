"""Shared language, voice and accompaniment contract for all video providers."""

import re

LANGUAGE_RULES = """SPOKEN DIALOGUE LANGUAGE
Follow any language explicitly requested for speech, lyrics, or vocals in the current user prompt, even if it differs from a stored voice profile or the defaults below. Apply the override only to the specified speakers, lines, or audio; preserve the voice's timbre and cadence. The language used to write the prompt does not itself specify the audio language.
Where the current user prompt leaves the language unspecified, assign it using these defaults and the character settings or asset descriptions.
1. All cartoon, anime and stylized animation characters speak Korean, regardless of their stated origin.
2. Human characters explicitly described as East Asian speak Korean.
3. Other human characters explicitly described as foreign or non-East-Asian speak English.
4. When the character setting is unspecified, use Korean.
Maintain the same assignment for each character throughout every shot. Preserve the intended meaning of requested dialogue, translating spoken lines into the assigned language. Keep spoken lines in their assigned language when the surrounding production prompt is English. Match audible speech and mouth movements to that language. Preserve the supplied character appearance. Keep explicitly silent scenes silent and add speech only when the scene requests it.
SONG AND VOCAL LANGUAGE
Apply the same language assignment to all intelligible sung words, lyrics, and chants, including background music. For a named character, use that character's assigned language consistently. For an unidentified singer, choir, or background vocal, default to Korean. Preserve the intended meaning of supplied lyrics in the assigned language. Keep instrumental music and wordless melodies free of added lyrics or speech. Respect explicit silence.
MULTI-SPEAKER VOICE CAST
When the script assigns lines to multiple characters, give each one a persistent speaker label tied to their asset identity and a distinct voice timbre, pitch range and speaking cadence consistent with the stated character settings. Keep each speaker's voice stable across close-ups, turns and shot changes.
Write requested dialogue line by line with its time range, speaker name and quoted words. Each solo line is audibly spoken by that named on-screen character with matching lip sync; the other characters listen or react. Preserve every requested speaker and line through prompt optimization. For explicitly shared greetings, layer all assigned individual voices together in unison, retaining the same voices used in their solo turns. Keep dialogue clearly audible above music and audience cheers. Restrict speaking roles and overlap to the script assignments."""


AUDIO_PRODUCTION_RULES = """VOCALS AND ACCOMPANIMENT
Default audio to requested character voices only. Add music, background music, instrumental accompaniment, beats or a backing track only when the user explicitly requests that audio. A dance, stage, cinematic mood or singing request alone does not authorize added music or accompaniment. A request to sing a song without a separate music or accompaniment request uses vocals only. Do not infer music permission from the previs, assets, automatically expanded scene description or genre. When music or accompaniment is explicitly requested, describe its style and balance under the requested voices. For a vocal performance, describe the identified performer's vocal language and voice. Do not invent speech or singing when no vocal performance is requested. Respect instrumental-only, wordless, a cappella, vocals-only and silent instructions. Preserve this user-requested audio scope through prompt expansion and optimization.
Singing, rapping, chanting and humming are vocal performances even when the dialogue list is empty. Give every vocal performer an active voice profile. Derive default language from explicit character settings and supplied descriptions, not the language of the prompt or a guessed nationality. Honor explicit user language overrides. State the selected language for each vocal performer in the final scene description and resolve conflicting language instructions before submitting. Keep named songs as requested context; do not claim that a title or URL supplies the original recording. These are model generation directions, not an uploaded audio track."""

MUSIC_STYLE_RULES = """KOREAN MUSIC STYLE DEFAULT
When generating music, default unspecified melody, rhythm, instrumentation, and arrangement to a natural Korean musical atmosphere appropriate to the scene. Contemporary Korean music is suitable; explicit user music styles and genres take precedence over this default. Do not force traditional Korean instruments or a traditional genre unless requested or appropriate to the scene. This style default does not authorize adding music, lyrics, or voices outside the requested audio scope. Do not restyle or regenerate uploaded audio; preserve the user's supplied recording as the priority background track."""

SPEECH_LANGUAGE_RULES = LANGUAGE_RULES + "\n" + AUDIO_PRODUCTION_RULES + "\n" + MUSIC_STYLE_RULES


def requires_vocal_profile(action):
    mode = action.get("vocal_mode")
    if mode is not None:
        if mode not in ("none", "speech", "singing", "humming", "chanting"):
            raise ValueError("Invalid vocal performance mode")
        if mode != "none":
            return True
    # Also reject contradictions between structured mode and direct actor actions.
    # Passive observations such as "listens to a singer" do not describe this actor singing.
    description = action.get("description", "")
    return bool(re.search(
        r"(?i)(?:^\s*|[.;,]\s*|\bwhile\s+|\b(?:starts|begins|continues|keeps|is|are)\s+(?:to\s+)?)"
        r"(?:he |she |they |the (?:singer|performer) )?"
        r"(?:sing(?:s|ing)?\b|rap(?:s|ping)?\b|chant(?:s|ing)?\b|hum(?:s|ming)?\b|노래한다|허밍한다|랩한다)",
        description))
