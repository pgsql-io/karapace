"""
karapace - schema tests

Copyright (c) 2019 Aiven Ltd
See LICENSE for details
"""
from http import HTTPStatus
from kafka import KafkaProducer
from karapace.rapu import is_success
from karapace.schema_registry_apis import KarapaceSchemaRegistry
from karapace.utils import Client
from tests.utils import create_field_name_factory, create_subject_name_factory, repeat_until_successful_request
from typing import List, Tuple

import json as jsonlib
import os
import pytest
import requests

baseurl = "http://localhost:8081"


@pytest.mark.parametrize("trail", ["", "/"])
async def test_compatibility_endpoint(registry_async_client: Client, trail: str) -> None:
    subject = create_subject_name_factory(f"test_compatibility_endpoint-{trail}")()

    res = await registry_async_client.put(f"config{trail}", json={"compatibility": "BACKWARD"})
    assert res.status == 200

    schema = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "age",
                "type": "int",
            },
        ]
    }

    res = await registry_async_client.post(
        f"subjects/{subject}/versions{trail}",
        json={"schema": jsonlib.dumps(schema)},
    )
    assert res.status == 200
    schema_id = res.json()['id']

    res = await registry_async_client.get(f"schemas/ids/{schema_id}{trail}")
    schema_gotten_back = jsonlib.loads(res.json()["schema"])
    assert schema_gotten_back == schema

    # replace int with long
    schema["fields"] = [{"type": "long", "name": "age"}]
    res = await registry_async_client.post(
        f"compatibility/subjects/{subject}/versions/latest{trail}",
        json={"schema": jsonlib.dumps(schema)},
    )
    assert res.status == 200
    assert res.json() == {"is_compatible": True}

    schema["fields"] = [{"type": "string", "name": "age"}]
    res = await registry_async_client.post(
        f"compatibility/subjects/{subject}/versions/latest{trail}",
        json={"schema": jsonlib.dumps(schema)},
    )
    assert res.status == 200
    assert res.json() == {"is_compatible": False}


@pytest.mark.parametrize("trail", ["", "/"])
async def test_record_schema_compatibility(registry_async_client: Client, trail: str) -> None:
    subject_name_factory = create_subject_name_factory(f"test_record_schema_compatibility-{trail}")
    subject_1 = subject_name_factory()

    res = await registry_async_client.put("config", json={"compatibility": "FORWARD"})
    assert res.status == 200
    schema = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "first_name",
                "type": "string",
            },
        ]
    }

    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema)},
    )
    assert res.status == 200
    assert "id" in res.json()
    schema_id = res.json()["id"]

    schema2 = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "first_name",
                "type": "string"
            },
            {
                "name": "last_name",
                "type": "string"
            },
            {
                "name": "age",
                "type": "int"
            },
        ]
    }
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema2)},
    )
    assert res.status == 200
    assert "id" in res.json()
    schema_id2 = res.json()["id"]
    assert schema_id != schema_id2

    schema3a = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "last_name",
                "type": "string"
            },
            {
                "name": "third_name",
                "type": "string",
                "default": "foodefaultvalue"
            },
            {
                "name": "age",
                "type": "int"
            },
        ]
    }
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema3a)},
    )
    # Fails because field removed
    assert res.status == 409
    res_json = res.json()
    assert res_json["error_code"] == 409

    schema3b = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "first_name",
                "type": "string"
            },
            {
                "name": "last_name",
                "type": "string"
            },
            {
                "name": "age",
                "type": "long"
            },
        ]
    }
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema3b)},
    )
    # Fails because incompatible type change
    assert res.status == 409
    res_json = res.json()
    assert res_json["error_code"] == 409

    schema4 = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "first_name",
                "type": "string"
            },
            {
                "name": "last_name",
                "type": "string"
            },
            {
                "name": "third_name",
                "type": "string",
                "default": "foodefaultvalue"
            },
            {
                "name": "age",
                "type": "int"
            },
        ]
    }
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema4)},
    )
    assert res.status == 200

    res = await registry_async_client.put("config", json={"compatibility": "BACKWARD"})
    schema5 = {
        "type": "record",
        "name": "Objct",
        "fields": [
            {
                "name": "first_name",
                "type": "string"
            },
            {
                "name": "last_name",
                "type": "string"
            },
            {
                "name": "third_name",
                "type": "string",
                "default": "foodefaultvalue"
            },
            {
                "name": "fourth_name",
                "type": "string"
            },
            {
                "name": "age",
                "type": "int"
            },
        ]
    }
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema5)},
    )
    assert res.status == 409

    # Add a default value for the field
    schema5["fields"][3] = {"name": "fourth_name", "type": "string", "default": "foof"}
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema5)},
    )
    assert res.status == 200
    assert "id" in res.json()

    # Try to submit schema with a different definition
    schema5["fields"][3] = {"name": "fourth_name", "type": "int", "default": 2}
    res = await registry_async_client.post(
        f"subjects/{subject_1}/versions{trail}",
        json={"schema": jsonlib.dumps(schema5)},
    )
    assert res.status == 409

    subject_2 = subject_name_factory()
    res = await registry_async_client.put(f"config/{subject_2}{trail}", json={"compatibility": "BACKWARD"})
    schema = {"type": "record", "name": "Object", "fields": [{"name": "first_name", "type": "string"}]}
    res = await registry_async_client.post(f"subjects/{subject_2}/versions{trail}", json={"schema": jsonlib.dumps(schema)})
    assert res.status == 200
    schema["fields"].append({"name": "last_name", "type": "string"})
    res = await registry_async_client.post(f"subjects/{subject_2}/versions{trail}", json={"schema": jsonlib.dumps(schema)})
    assert res.status == 409

