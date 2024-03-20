# Copyright Modal Labs 2022
import pytest
import time
from unittest import mock

import modal
from modal.exception import DeprecationError, InvalidError, NotFoundError
from modal.runner import deploy_stub


def dummy():
    pass


def test_network_file_system_files(client, test_dir, servicer):
    stub = modal.Stub()
    nfs = modal.NetworkFileSystem.from_name("xyz", create_if_missing=True)

    dummy_modal = stub.function(network_file_systems={"/root/foo": nfs})(dummy)

    with stub.run(client=client):
        dummy_modal.remote()


def test_network_file_system_bad_paths():
    stub = modal.Stub()
    nfs = modal.NetworkFileSystem.from_name("xyz", create_if_missing=True)

    def _f():
        pass

    with pytest.raises(InvalidError):
        stub.function(network_file_systems={"/root/../../foo": nfs})(dummy)

    with pytest.raises(InvalidError):
        stub.function(network_file_systems={"/": nfs})(dummy)

    with pytest.raises(InvalidError):
        stub.function(network_file_systems={"/tmp/": nfs})(dummy)


def test_network_file_system_handle_single_file(client, tmp_path, servicer):
    local_file_path = tmp_path / "some_file"
    local_file_path.write_text("hello world")

    with modal.NetworkFileSystem.ephemeral(client=client) as nfs:
        nfs.add_local_file(local_file_path)
        nfs.add_local_file(local_file_path.as_posix(), remote_path="/foo/other_destination")
        object_id = nfs.object_id

    assert servicer.nfs_files[object_id].keys() == {
        "/some_file",
        "/foo/other_destination",
    }
    assert servicer.nfs_files[object_id]["/some_file"].data == b"hello world"
    assert servicer.nfs_files[object_id]["/foo/other_destination"].data == b"hello world"


@pytest.mark.asyncio
async def test_network_file_system_handle_dir(client, tmp_path, servicer):
    local_dir = tmp_path / "some_dir"
    local_dir.mkdir()
    (local_dir / "smol").write_text("###")

    subdir = local_dir / "subdir"
    subdir.mkdir()
    (subdir / "other").write_text("####")

    with modal.NetworkFileSystem.ephemeral(client=client) as nfs:
        nfs.add_local_dir(local_dir)
        object_id = nfs.object_id

    assert servicer.nfs_files[object_id].keys() == {
        "/some_dir/smol",
        "/some_dir/subdir/other",
    }
    assert servicer.nfs_files[object_id]["/some_dir/smol"].data == b"###"
    assert servicer.nfs_files[object_id]["/some_dir/subdir/other"].data == b"####"


@pytest.mark.asyncio
async def test_network_file_system_handle_big_file(client, tmp_path, servicer, blob_server, *args):
    with mock.patch("modal.network_file_system.LARGE_FILE_LIMIT", 10):
        local_file_path = tmp_path / "bigfile"
        local_file_path.write_text("hello world, this is a lot of text")

        async with modal.NetworkFileSystem.ephemeral(client=client) as nfs:
            await nfs.add_local_file.aio(local_file_path)
            object_id = nfs.object_id

        assert servicer.nfs_files[object_id].keys() == {"/bigfile"}
        assert servicer.nfs_files[object_id]["/bigfile"].data == b""
        assert servicer.nfs_files[object_id]["/bigfile"].data_blob_id == "bl-1"

        _, blobs = blob_server
        assert blobs["bl-1"] == b"hello world, this is a lot of text"


def test_old_syntax(client, servicer):
    stub = modal.Stub()
    with pytest.raises(DeprecationError):
        stub.vol1 = modal.SharedVolume()
    with pytest.raises(DeprecationError):
        stub.vol2 = modal.SharedVolume.new()


def test_redeploy(servicer, client):
    stub = modal.Stub()
    with pytest.warns(DeprecationError):
        stub.n1 = modal.NetworkFileSystem.new()
        stub.n2 = modal.NetworkFileSystem.new()
        stub.n3 = modal.NetworkFileSystem.new()

    # Deploy app once
    deploy_stub(stub, "my-app", client=client)
    app1_ids = [stub.n1.object_id, stub.n2.object_id, stub.n3.object_id]

    # Deploy app again
    deploy_stub(stub, "my-app", client=client)
    app2_ids = [stub.n1.object_id, stub.n2.object_id, stub.n3.object_id]

    # Make sure ids are stable
    assert app1_ids == app2_ids

    # Make sure ids are unique
    assert len(set(app1_ids)) == 3
    assert len(set(app2_ids)) == 3

    # Deploy to a different app
    deploy_stub(stub, "my-other-app", client=client)
    app3_ids = [stub.n1.object_id, stub.n2.object_id, stub.n3.object_id]

    # Should be unique and different
    assert len(set(app3_ids)) == 3
    assert set(app1_ids) & set(app3_ids) == set()


def test_read_file(client, tmp_path, servicer):
    with modal.NetworkFileSystem.ephemeral(client=client) as nfs:
        with pytest.raises(FileNotFoundError):
            for _ in nfs.read_file("idontexist.txt"):
                ...


def test_write_file(client, tmp_path, servicer):
    local_file_path = tmp_path / "some_file"
    local_file_path.write_text("hello world")

    with modal.NetworkFileSystem.ephemeral(client=client) as nfs:
        nfs.write_file("remote_path.txt", open(local_file_path, "rb"))

        # Make sure we can write through the provider too
        nfs.write_file("remote_path.txt", open(local_file_path, "rb"))


def test_persisted(servicer, client):
    # Lookup should fail since it doesn't exist
    with pytest.raises(NotFoundError):
        modal.NetworkFileSystem.lookup("xyz", client=client)

    # Create it
    modal.NetworkFileSystem.lookup("xyz", create_if_missing=True, client=client)

    # Lookup should succeed now
    modal.NetworkFileSystem.lookup("xyz", client=client)


def test_nfs_ephemeral(servicer, client, tmp_path):
    local_file_path = tmp_path / "some_file"
    local_file_path.write_text("hello world")

    assert servicer.n_nfs_heartbeats == 0
    with modal.NetworkFileSystem.ephemeral(client=client, _heartbeat_sleep=1) as nfs:
        assert nfs.listdir("/") == []
        nfs.write_file("xyz.txt", open(local_file_path, "rb"))
        (entry,) = nfs.listdir("/")
        assert entry.path == "xyz.txt"

        time.sleep(1.5)  # Make time for 2 heartbeats
    assert servicer.n_nfs_heartbeats == 2
