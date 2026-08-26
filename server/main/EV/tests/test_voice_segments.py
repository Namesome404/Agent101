from speech.voice_core.segments import split_ready_segments


def _stream_segments(text, *, first_chars=18, next_chars=42):
    pending = ""
    first = True
    segments = []
    for character in text:
        pending += character
        ready, pending = split_ready_segments(
            pending,
            first,
            first_chars=first_chars,
            next_chars=next_chars,
        )
        segments.extend(ready)
        if ready:
            first = False
    if pending.strip():
        segments.append(pending.strip())
    return segments


def test_streaming_english_waits_for_sentence_boundaries():
    assert _stream_segments(
        "Understood. English it is. What's on your mind?"
    ) == ["Understood.", "English it is.", "What's on your mind?"]


def test_streaming_english_never_cuts_a_word_at_the_length_threshold():
    segments = _stream_segments(
        "This deliberately unpunctuated English response stays intact",
        first_chars=8,
        next_chars=12,
    )

    assert segments == [
        "This deliberately unpunctuated English response stays intact"
    ]


def test_decimal_point_is_not_a_sentence_boundary():
    assert _stream_segments("It is 21.6 degrees. Bring an umbrella.") == [
        "It is 21.6 degrees.",
        "Bring an umbrella.",
    ]


def test_common_english_abbreviation_is_not_a_sentence_boundary():
    assert _stream_segments("Dr. Smith is ready. The room is open.") == [
        "Dr. Smith is ready.",
        "The room is open.",
    ]
