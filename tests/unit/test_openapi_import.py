import copy

import pytest

from orchivio.integrations.openapi_import import ImportProblem, import_openapi, pascal, snake
from orchivio.model import AppModel, assign_element_ids, has_errors, migrate_to_latest, validate_model

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Pet Shop", "version": "1"},
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                    {"name": "tags", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}},
                ],
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}},
            },
            "post": {
                "operationId": "createPet",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewPet"}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}},
            },
        },
        "/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getPetById", "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}},
            "delete": {"responses": {"204": {"description": "gone"}}},
        },
        "/upload": {"post": {"operationId": "upload", "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                             "responses": {"200": {"description": "ok"}}}},
        "/health": {"get": {"responses": {"200": {"content": {"text/plain": {"schema": {"type": "string"}}}}}}},
    },
    "components": {"schemas": {
        "Pet": {"allOf": [{"$ref": "#/components/schemas/NewPet"},
                           {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "createdAt": {"type": "string", "format": "date-time"},
                                                                                  "owner": {"type": "object"}, "score": {"type": "number"}}}]},
        "NewPet": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string", "maxLength": 80}, "tag": {"type": ["string", "null"]},
                                                                           "isGood": {"type": "boolean"}}},
    }},
}


def run(spec=SPEC, **kw):
    return import_openapi(copy.deepcopy(spec), key="pets", destination="petshop", **kw)


def op(result, oid):
    return next(o for o in result.integration.operations if o.id == oid)


def test_names():
    assert snake("petId") == "pet_id" and snake("X-Request-ID") == "x_request_id" and snake("123abc", "param") == "p_123abc"
    assert snake("") == "field" and pascal("pet_store-item") == "PetStoreItem" and pascal("9lives") == "Entity9lives"


def test_operations_parameters_and_wire_names():
    r = run()
    assert r.integration.key == "pets" and r.integration.destination == "petshop" and r.integration.description == "Pet Shop"
    ids = [o.id for o in r.integration.operations]
    assert ids == ["list_pets", "create_pet", "get_pet_by_id", "delete_pets_pet_id", "get_health"]  # missing operationId is derived
    lp = op(r, "list_pets")
    assert [(p.name, p.location, p.type, p.required) for p in lp.params] == [("limit", "query", "integer", False)]
    get = op(r, "get_pet_by_id")
    assert get.path == "/pets/{pet_id}" and get.params[0].name == "pet_id" and get.params[0].json_name == "petId" and get.params[0].required
    assert get.method == "GET" and op(r, "delete_pets_pet_id").response is None


def test_bodies_and_responses_become_transient_entities():
    r = run()
    names = {e.name: e for e in r.entities}
    assert set(names) == {"Pet", "NewPet"} and all(e.transient for e in r.entities)
    pet = {f.name: f for f in names["Pet"].fields}
    assert pet["id"].type == "integer" and pet["id"].required and pet["created_at"].type == "datetime" and pet["owner"].type == "json"
    assert pet["score"].type == "decimal" and pet["name"].max_length == 80 and pet["name"].required  # allOf merged
    lp, cp = op(r, "list_pets"), op(r, "create_pet")
    assert (lp.response, lp.response_kind) == ("Pet", "list") and lp.response_map["created_at"] == "createdAt"
    assert (cp.body, cp.response, cp.response_kind) == ("NewPet", "Pet", "object") and cp.body_map == {"is_good": "isGood"}
    assert pet["created_at"].type == "datetime" and {f.name for f in names["NewPet"].fields} == {"name", "tag", "is_good"}


def test_what_is_left_out_is_reported():
    r = run()
    assert {s["operation"] for s in r.skipped} == {"POST /upload"}  # required non-JSON body: cannot be imported
    text = " ".join(r.warnings)
    assert "header parameter 'X-Trace'" in text and "tags" in text and "non-JSON response" in text


def test_taken_entity_names_are_never_reused():
    r = run(taken_entities={"Pet", "NewPet"})
    assert {e.name for e in r.entities} == {"Pet2", "NewPet2"}
    assert op(r, "list_pets").response == "Pet2"


@pytest.mark.parametrize("doc, why", [
    ("nope", "object"), ({"swagger": "2.0", "paths": {}}, "OpenAPI 3"), ({"openapi": "3.0.0"}, "no paths"), ({"openapi": "3.0.0", "paths": {}}, "no paths"),
    ({"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "https://evil.test/x.json"}}}}}}}}}, "local references"),
    ({"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Nope"}}}}}}}}}, "unresolvable"),
    ({"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}}}}}},
      "components": {"schemas": {"A": {"$ref": "#/components/schemas/A"}}}}, "deeply"),
])
def test_unusable_documents(doc, why):
    with pytest.raises(ImportProblem, match=why):
        import_openapi(doc, key="x", destination="y")


def test_limits():
    paths = {f"/p{i}": {"get": {"operationId": f"op{i}", "responses": {"200": {"description": "ok"}}}} for i in range(230)}
    r = import_openapi({"openapi": "3.1.0", "paths": paths}, key="x", destination="y")
    assert len(r.integration.operations) == 200 and len(r.skipped) == 30
    wide = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(150)}}
    spec = {"openapi": "3.0.0", "paths": {"/w": {"get": {"responses": {"200": {"content": {"application/json": {"schema": wide}}}}}}}}
    r = import_openapi(spec, key="x", destination="y")
    assert len(r.entities[0].fields) == 100 and any("more than 100" in w for w in r.warnings)


def test_colliding_wire_names_get_distinct_identifiers():
    spec = {"openapi": "3.0.0", "paths": {"/a": {"get": {"operationId": "a", "parameters": [
        {"name": "userId", "in": "query", "schema": {"type": "string"}}, {"name": "user_id", "in": "query", "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}}}}}}
    r = import_openapi(spec, key="x", destination="y")
    assert [p.name for p in r.integration.operations[0].params] == ["user_id", "user_id_2"]
    assert [p.json_name for p in r.integration.operations[0].params] == ["userId", "user_id"] or r.integration.operations[0].params[1].json_name is None


def test_the_result_is_a_valid_model(raw_model):
    r = run()
    m = copy.deepcopy(raw_model)
    m["modules"][0]["entities"] += [e.model_dump(mode="json", exclude_none=True) for e in r.entities]
    m["modules"][0]["integrations"] = [r.integration.model_dump(mode="json", exclude_none=True)]
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(m)))
    errors = [i for i in validate_model(model) if i.severity == "error"]
    assert not has_errors(errors), errors
