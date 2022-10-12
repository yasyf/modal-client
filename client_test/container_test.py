import platform
import pytest
import sys
import time

from grpclib.exceptions import GRPCError

from modal._container_entrypoint import UserException, main

# from modal_test_support import SLEEP_DELAY
from modal._serialization import deserialize, serialize
from modal.client import Client
from modal.exception import InvalidError
from modal_proto import api_pb2

EXTRA_TOLERANCE_DELAY = 1.0
FUNCTION_CALL_ID = "fc-123"
SLEEP_DELAY = 0.1


skip_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Windows doesn't have UNIX sockets",
)


def _get_inputs(args=((42,), {})):
    input_pb = api_pb2.FunctionInput(args=serialize(args))

    return [
        api_pb2.FunctionGetInputsResponse(inputs=[api_pb2.FunctionGetInputsItem(input_id="in-xyz", input=input_pb)]),
        api_pb2.FunctionGetInputsResponse(inputs=[api_pb2.FunctionGetInputsItem(kill_switch=True)]),
    ]


def _get_output(function_output_req: api_pb2.FunctionPutOutputsRequest) -> api_pb2.GenericResult:
    assert len(function_output_req.outputs) == 1
    return function_output_req.outputs[0].result


def _run_container(
    servicer,
    module_name,
    function_name,
    fail_get_inputs=False,
    inputs=None,
    function_type=api_pb2.Function.FUNCTION_TYPE_FUNCTION,
):
    with Client(servicer.remote_addr, api_pb2.CLIENT_TYPE_CONTAINER, ("ta-123", "task-secret")) as client:
        if inputs is None:
            servicer.container_inputs = _get_inputs()
        else:
            servicer.container_inputs = inputs
        servicer.fail_get_inputs = fail_get_inputs

        function_def = api_pb2.Function(
            module_name=module_name,
            function_name=function_name,
            function_type=function_type,
        )

        # Note that main is a synchronous function, so we need to run it in a separate thread
        container_args = api_pb2.ContainerArguments(
            task_id="ta-123",
            function_id="fu-123",
            app_id="se-123",
            function_def=function_def,
        )

        try:
            main(container_args, client)
        except UserException:
            # Handle it gracefully
            pass

        return client, servicer.container_outputs


@skip_windows
def test_container_entrypoint_success(unix_servicer, event_loop):
    t0 = time.time()
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "square")
    assert 0 <= time.time() - t0 < EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_windows
def test_container_entrypoint_generator_success(unix_servicer, event_loop):
    client, output_requests = _run_container(
        unix_servicer, "modal_test_support.functions", "gen_n", function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR
    )

    assert 1 <= len(output_requests) <= 43
    all_output_items = []
    for req in output_requests:
        all_output_items += list(req.outputs)

    assert len(all_output_items) == 43  # The generator creates N outputs, and N is 42 from the autogenerated input

    for i in range(42):
        result = all_output_items[i].result
        assert result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
        assert result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_INCOMPLETE
        assert deserialize(result.data, client) == i**2
        assert all_output_items[i].gen_index == i

    last_result = all_output_items[-1].result
    assert last_result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert last_result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_COMPLETE
    assert last_result.data == b""  # no data in generator complete marker result


@skip_windows
def test_container_entrypoint_generator_failure(unix_servicer, event_loop):
    inputs = _get_inputs(((10, 5), {}))
    client, output_requests = _run_container(
        unix_servicer,
        "modal_test_support.functions",
        "gen_n_fail_on_m",
        function_type=api_pb2.Function.FUNCTION_TYPE_GENERATOR,
        inputs=inputs,
    )

    assert 1 <= len(output_requests) <= 11
    all_output_items = []
    for req in output_requests:
        all_output_items += list(req.outputs)

    assert len(all_output_items) == 6  # 5 successful outputs, 1 failure

    for i in range(5):
        result = all_output_items[i].result
        assert result.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
        assert result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_INCOMPLETE
        assert deserialize(result.data, client) == i**2
        assert all_output_items[i].gen_index == i

    last_result = all_output_items[-1].result
    assert last_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert last_result.gen_status == api_pb2.GenericResult.GENERATOR_STATUS_UNSPECIFIED
    data = deserialize(last_result.data, client)
    assert isinstance(data, Exception)
    assert data.args == ("bad",)


@skip_windows
def test_container_entrypoint_async(unix_servicer):
    t0 = time.time()
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "square_async")
    assert SLEEP_DELAY <= time.time() - t0 < SLEEP_DELAY + EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_windows
def test_container_entrypoint_failure(unix_servicer):
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "raises")

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert output.exception == "Exception('Failure!')"
    assert "Traceback" in output.traceback


@skip_windows
def test_container_entrypoint_raises_base_exception(unix_servicer):
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "raises_sysexit")

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert output.exception == "SystemExit(1)"


@skip_windows
def test_container_entrypoint_keyboardinterrupt(unix_servicer):
    with pytest.raises(KeyboardInterrupt):
        client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "raises_keyboardinterrupt")


@skip_windows
def test_container_entrypoint_rate_limited(unix_servicer, event_loop):
    t0 = time.time()
    unix_servicer.rate_limit_sleep_duration = 0.25
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "square")
    assert 0.25 <= time.time() - t0 < 0.25 + EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_windows
def test_container_entrypoint_grpc_failure(unix_servicer, event_loop):
    # An error in "Modal code" should cause the entire container to fail
    with pytest.raises(GRPCError):
        _run_container(unix_servicer, "modal_test_support.functions", "square", fail_get_inputs=True)

    # assert unix_servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    # assert "GRPCError" in unix_servicer.task_result.exception


@skip_windows
def test_container_entrypoint_missing_main_conditional(unix_servicer, event_loop):
    _run_container(unix_servicer, "modal_test_support.missing_main_conditional", "square")

    assert unix_servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert 'if __name__ == "__main__":' in unix_servicer.task_result.traceback

    exc = deserialize(unix_servicer.task_result.data, None)
    assert isinstance(exc, InvalidError)


@skip_windows
def test_container_entrypoint_startup_failure(unix_servicer, event_loop):
    _run_container(unix_servicer, "modal_test_support.startup_failure", "f")

    assert unix_servicer.task_result.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE

    exc = deserialize(unix_servicer.task_result.data, None)
    assert isinstance(exc, ImportError)


@skip_windows
def test_container_entrypoint_class_scoped_function(unix_servicer, event_loop):
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "Cube.f")
    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**3)

    Cube = sys.modules["modal_test_support.functions"].Cube  # don't redefine

    assert Cube._events == ["init", "enter", "call", "exit"]


@skip_windows
def test_container_entrypoint_class_scoped_function_async(unix_servicer, event_loop):
    client, outputs = _run_container(unix_servicer, "modal_test_support.functions", "CubeAsync.f")
    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**3)

    CubeAsync = sys.modules["modal_test_support.functions"].CubeAsync

    assert CubeAsync._events == ["init", "enter", "call", "exit"]


@skip_windows
def test_create_package_mounts_inside_container(unix_servicer, event_loop):
    """`create_package_mounts` shouldn't actually run inside the container, because it's possible
    that there are modules that were present locally for the user that didn't get mounted into
    all the containers."""

    client, outputs = _run_container(unix_servicer, "modal_test_support.package_mount", "num_mounts")

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(0)
