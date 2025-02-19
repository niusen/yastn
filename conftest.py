import pytest


def pytest_addoption(parser):
    parser.addoption("--backend", help='backend', default='np', choices=['np','torch','torch_cpp'],\
     action='store')
    parser.addoption("--device", help='cpu or cuda', default='cpu', action='store')
    parser.addoption("--tensordot_policy", help='fuse_to_matrix or fuse_contracted or direct', default='fuse_to_matrix', action='store')
    parser.addoption("--bug_pytorch110", help='test complex conjugation bug in PyTorch 1.10',\
        action='store_true', dest="bug_pytorch110", default=False)
    parser.addoption("--quickstart", help='execute quickstarts',\
        action='store_true', dest="quickstart", default=False)
    parser.addoption("--long_tests", help='run long duration tests', action='store_true', default=False)


@pytest.fixture
def config_kwargs(request):
    return {'backend': request.config.getoption("--backend"),
            'default_device': request.config.getoption("--device"),
            'tensordot_policy': request.config.getoption("--tensordot_policy")}
