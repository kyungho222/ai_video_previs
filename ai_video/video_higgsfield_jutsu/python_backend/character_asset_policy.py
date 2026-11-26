"""Declared character-reference format shared by analysis and generation."""
SINGLE_CHARACTER_RULE = (
    'CHARACTER REFERENCE FORMAT: Each character asset image represents ONE person. '
    'Headless outfit front/back views and the frontal face portrait in that image are '
    'complementary references for that SAME person, never separate people. Keep the entire '
    'image together; combine its face and wardrobe into one complete character. '
    'Do not split panels into identities or reproduce headless bodies, sheets or panels in the scene.'
)


def single_character_parts(assets):
    return [{'sourceIndex':i} for i in range(1,len(assets)+1)]
