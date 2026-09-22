import re

import holdfast


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", holdfast.__version__)
