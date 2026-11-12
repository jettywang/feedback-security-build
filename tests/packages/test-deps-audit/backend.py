def get_requires_for_build_wheel(config_settings):
    return ['backend-dep', 'shared-dep>=2', 'not a requirement either']


def get_requires_for_build_sdist(config_settings):
    return ['backend-dep']
