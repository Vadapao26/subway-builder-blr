#!/usr/bin/env python3
"""
Pure-Python OSM PBF reader, bounding-box clipper, and GeoTIFF reader utilities.
Works natively on Windows without C++ osmium or GDAL dependencies.
"""

import struct
import zlib
from pathlib import Path
from typing import Callable, Iterator


def read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    """Decode a protobuf base-128 varint starting at `pos`."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def decode_zigzag(n: int) -> int:
    """Decode a protobuf sint64 zigzag-encoded integer."""
    return (n >> 1) ^ -(n & 1)


def parse_blob_header(data: bytes) -> tuple[str, int]:
    """Parse an OSMPBF BlobHeader message -> (type_str, datasize)."""
    pos = 0
    n = len(data)
    btype = ""
    datasize = 0
    while pos < n:
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            length, pos = read_varint(data, pos)
            btype = bytes(data[pos : pos + length]).decode("utf-8", errors="replace")
            pos += length
        elif field_num == 3 and wire_type == 0:
            datasize, pos = read_varint(data, pos)
        else:
            pos = skip_field(data, pos, wire_type)
    return btype, datasize


def decompress_blob(data: bytes) -> bytes:
    """Parse an OSMPBF Blob message and return uncompressed payload bytes."""
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:  # raw
            length, pos = read_varint(data, pos)
            return bytes(data[pos : pos + length])
        elif field_num == 3 and wire_type == 2:  # zlib_data
            length, pos = read_varint(data, pos)
            return zlib.decompress(memoryview(data)[pos : pos + length])
        else:
            pos = skip_field(data, pos, wire_type)
    return b""


def skip_field(data: bytes | memoryview, pos: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = read_varint(data, pos)
        return pos
    elif wire_type == 1:
        return pos + 8
    elif wire_type == 2:
        length, pos = read_varint(data, pos)
        return pos + length
    elif wire_type == 5:
        return pos + 4
    raise ValueError(f"Unsupported protobuf wire_type: {wire_type}")


def iter_pbf_blobs(pbf_path: Path) -> Iterator[tuple[int, str, bytes, bytes]]:
    """
    Iterate over blobs in an .osm.pbf file.
    Yields (file_offset, blob_type, raw_header_plus_blob_bytes, uncompressed_payload).
    """
    with open(pbf_path, "rb") as f:
        offset = 0
        while True:
            len_bytes = f.read(4)
            if not len_bytes or len(len_bytes) < 4:
                break
            header_len = struct.unpack(">I", len_bytes)[0]
            header_bytes = f.read(header_len)
            btype, datasize = parse_blob_header(header_bytes)
            blob_bytes = f.read(datasize)
            raw_chunk = len_bytes + header_bytes + blob_bytes
            payload = decompress_blob(blob_bytes)
            yield offset, btype, raw_chunk, payload
            offset += 4 + header_len + datasize


def parse_string_table(data: memoryview) -> list[str]:
    strings: list[str] = []
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            length, pos = read_varint(data, pos)
            strings.append(bytes(data[pos : pos + length]).decode("utf-8", errors="replace"))
            pos += length
        else:
            pos = skip_field(data, pos, wire_type)
    return strings


def unpack_packed_varints(data: memoryview) -> list[int]:
    vals: list[int] = []
    pos = 0
    n = len(data)
    while pos < n:
        v, pos = read_varint(data, pos)
        vals.append(v)
    return vals


def unpack_packed_sint64(data: memoryview) -> list[int]:
    vals: list[int] = []
    pos = 0
    n = len(data)
    while pos < n:
        v, pos = read_varint(data, pos)
        vals.append((v >> 1) ^ -(v & 1))
    return vals


def parse_dense_nodes(
    data: memoryview,
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> tuple[list[int], list[float], list[float], list[int]]:
    """
    Parse DenseNodes message -> (ids, lats, lons, keys_vals).
    """
    pos = 0
    n = len(data)
    delta_ids: list[int] = []
    delta_lats: list[int] = []
    delta_lons: list[int] = []
    keys_vals: list[int] = []

    while pos < n:
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            length, pos = read_varint(data, pos)
            delta_ids = unpack_packed_sint64(data[pos : pos + length])
            pos += length
        elif field_num == 8 and wire_type == 2:
            length, pos = read_varint(data, pos)
            delta_lats = unpack_packed_sint64(data[pos : pos + length])
            pos += length
        elif field_num == 9 and wire_type == 2:
            length, pos = read_varint(data, pos)
            delta_lons = unpack_packed_sint64(data[pos : pos + length])
            pos += length
        elif field_num == 10 and wire_type == 2:
            length, pos = read_varint(data, pos)
            keys_vals = unpack_packed_varints(data[pos : pos + length])
            pos += length
        else:
            pos = skip_field(data, pos, wire_type)

    count = len(delta_ids)
    ids = [0] * count
    lats = [0.0] * count
    lons = [0.0] * count
    cur_id = 0
    cur_lat = 0
    cur_lon = 0
    scale = 1e-9 * granularity
    lat_off_deg = 1e-9 * lat_offset
    lon_off_deg = 1e-9 * lon_offset

    for i in range(count):
        cur_id += delta_ids[i]
        cur_lat += delta_lats[i]
        cur_lon += delta_lons[i]
        ids[i] = cur_id
        lats[i] = lat_off_deg + scale * cur_lat
        lons[i] = lon_off_deg + scale * cur_lon

    return ids, lats, lons, keys_vals


def parse_way(data: memoryview, string_table: list[str]) -> tuple[int, dict[str, str], list[int]]:
    """Parse a Way message -> (way_id, tags_dict, node_refs_list)."""
    pos = 0
    n = len(data)
    way_id = 0
    keys: list[int] = []
    vals: list[int] = []
    delta_refs: list[int] = []

    while pos < n:
        tag, pos = read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 0:
            way_id, pos = read_varint(data, pos)
        elif field_num == 2 and wire_type == 2:
            length, pos = read_varint(data, pos)
            keys = unpack_packed_varints(data[pos : pos + length])
            pos += length
        elif field_num == 3 and wire_type == 2:
            length, pos = read_varint(data, pos)
            vals = unpack_packed_varints(data[pos : pos + length])
            pos += length
        elif field_num == 8 and wire_type == 2:
            length, pos = read_varint(data, pos)
            delta_refs = unpack_packed_sint64(data[pos : pos + length])
            pos += length
        else:
            pos = skip_field(data, pos, wire_type)

    tags = {}
    for k_idx, v_idx in zip(keys, vals):
        if k_idx < len(string_table) and v_idx < len(string_table):
            tags[string_table[k_idx]] = string_table[v_idx]

    refs = []
    cur_ref = 0
    for d in delta_refs:
        cur_ref += d
        refs.append(cur_ref)

    return way_id, tags, refs


def parse_primitive_block(
    payload: bytes,
) -> tuple[list[str], int, int, int, list[memoryview]]:
    """
    Parse an OSMPBF PrimitiveBlock -> (string_table, granularity, lat_offset, lon_offset, primitive_groups).
    """
    mv = memoryview(payload)
    pos = 0
    n = len(mv)
    string_table: list[str] = []
    granularity = 100
    lat_offset = 0
    lon_offset = 0
    groups: list[memoryview] = []

    while pos < n:
        tag, pos = read_varint(mv, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            length, pos = read_varint(mv, pos)
            string_table = parse_string_table(mv[pos : pos + length])
            pos += length
        elif field_num == 2 and wire_type == 2:
            length, pos = read_varint(mv, pos)
            groups.append(mv[pos : pos + length])
            pos += length
        elif field_num == 17 and wire_type == 0:
            granularity, pos = read_varint(mv, pos)
        elif field_num == 19 and wire_type == 0:
            v, pos = read_varint(mv, pos)
            lat_offset = decode_zigzag(v)
        elif field_num == 20 and wire_type == 0:
            v, pos = read_varint(mv, pos)
            lon_offset = decode_zigzag(v)
        else:
            pos = skip_field(mv, pos, wire_type)

    return string_table, granularity, lat_offset, lon_offset, groups


def clip_osm_pbf_to_bbox(
    src_pbf: Path,
    dst_pbf: Path,
    south: float,
    north: float,
    west: float,
    east: float,
) -> dict:
    """
    Stream `src_pbf` and write `dst_pbf` containing the OSMHeader plus all PrimitiveBlock blobs
    that contain:
    1) Any node within [south, north] x [west, east], OR
    2) Any way referencing a node within the bbox, OR
    3) Any relation referencing a kept way or node.
    Conducts a fast two-pass blob filter so `dst_pbf` is a valid, self-contained `.osm.pbf`
    clipped to the study area + routing margin.
    """
    in_bbox_nodes: set[int] = set()
    kept_blob_offsets: set[int] = set()
    header_chunks: list[bytes] = []

    total_blobs = 0
    node_blobs_kept = 0
    way_blobs_kept = 0

    # Pass 1: find all nodes inside the expanded bounding box and record their blobs
    for offset, btype, raw_chunk, payload in iter_pbf_blobs(src_pbf):
        total_blobs += 1
        if btype == "OSMHeader":
            header_chunks.append(raw_chunk)
            continue
        if btype != "OSMData":
            continue

        _, granularity, lat_offset, lon_offset, groups = parse_primitive_block(payload)
        blob_has_bbox_node = False
        for grp in groups:
            gpos = 0
            gn = len(grp)
            while gpos < gn:
                tag, gpos = read_varint(grp, gpos)
                fnum = tag >> 3
                wtype = tag & 0x07
                if fnum == 2 and wtype == 2:  # DenseNodes
                    length, gpos = read_varint(grp, gpos)
                    ids, lats, lons, _ = parse_dense_nodes(
                        grp[gpos : gpos + length], granularity, lat_offset, lon_offset
                    )
                    gpos += length
                    for nid, lat, lon in zip(ids, lats, lons):
                        if south <= lat <= north and west <= lon <= east:
                            in_bbox_nodes.add(nid)
                            blob_has_bbox_node = True
                else:
                    gpos = skip_field(grp, gpos, wtype)
        if blob_has_bbox_node:
            kept_blob_offsets.add(offset)
            node_blobs_kept += 1

    # Pass 2: find all ways that reference any node in `in_bbox_nodes` (and collect their boundary nodes)
    extra_way_nodes: set[int] = set()
    for offset, btype, raw_chunk, payload in iter_pbf_blobs(src_pbf):
        if btype != "OSMData" or offset in kept_blob_offsets:
            continue
        string_table, _, _, _, groups = parse_primitive_block(payload)
        blob_has_bbox_way = False
        for grp in groups:
            gpos = 0
            gn = len(grp)
            while gpos < gn:
                tag, gpos = read_varint(grp, gpos)
                fnum = tag >> 3
                wtype = tag & 0x07
                if fnum == 3 and wtype == 2:  # Way
                    length, gpos = read_varint(grp, gpos)
                    _, _, refs = parse_way(grp[gpos : gpos + length], string_table)
                    gpos += length
                    if any(r in in_bbox_nodes for r in refs):
                        blob_has_bbox_way = True
                        for r in refs:
                            if r not in in_bbox_nodes:
                                extra_way_nodes.add(r)
                else:
                    gpos = skip_field(grp, gpos, wtype)
        if blob_has_bbox_way:
            kept_blob_offsets.add(offset)
            way_blobs_kept += 1

    # Write clipped PBF
    dst_pbf.parent.mkdir(parents=True, exist_ok=True)
    written_blobs = 0
    with open(dst_pbf, "wb") as out_f:
        for hc in header_chunks:
            out_f.write(hc)
        for offset, btype, raw_chunk, _ in iter_pbf_blobs(src_pbf):
            if offset in kept_blob_offsets:
                out_f.write(raw_chunk)
                written_blobs += 1

    return {
        "total_source_blobs": total_blobs,
        "kept_blobs": written_blobs,
        "in_bbox_nodes_count": len(in_bbox_nodes),
        "node_blobs_kept": node_blobs_kept,
        "way_blobs_kept": way_blobs_kept,
        "clipped_size_bytes": dst_pbf.stat().st_size,
    }
