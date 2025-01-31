import time
import unittest
from urlparse import urljoin
import uuid

import requests

from apmserver import ElasticTest
from beat.beat import INTEGRATION_TESTS


class AgentConfigurationTest(ElasticTest):
    config_overrides = {
        "logging_json": "true",
        "kibana_enabled": "true",
        "acm_cache_expiration": "1s",
    }

    def config(self):
        cfg = super(ElasticTest, self).config()
        cfg.update({
            "kibana_host": self.get_kibana_url(),
        })
        cfg.update(self.config_overrides)
        return cfg

    def _upsert_service_config(self, settings, name, agent="python", env=None, _id="new"):
        data = {
            "agent_name": agent,
            "service": {"name": name},
            "settings": settings
        }
        if env is not None:
            data["service"]["environment"] = env

        method = requests.post if _id == "new" else requests.put
        return method(
            urljoin(self.kibana_url, "/api/apm/settings/agent-configuration/{}".format(_id)),
            headers={
                "Accept": "*/*",
                "Content-Type": "application/json",
                "kbn-xsrf": "1",
            },
            json=data,
        )

    def create_service_config(self, settings, name, agent="python", env=None):
        config = self._upsert_service_config(settings, name, agent=agent, env=env)
        config.raise_for_status()
        assert config.status_code == 200, config.status_code
        assert config.json()["result"] == "created"
        return config.json()["_id"]

    def update_service_config(self, config_id, settings, name, env=None):
        config = self._upsert_service_config(settings, name, env=env, _id=config_id)
        assert config.status_code == 200, config.status_code
        assert config.json()["result"] == "updated"


class AgentConfigurationIntegrationTest(AgentConfigurationTest):

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_config_requests(self):
        service_name = uuid.uuid4().hex
        service_env = "production"
        bad_service_env = "notreal"

        expect_log = []

        # missing service.name
        r1 = requests.get(self.agent_config_url,
                          headers={"Content-Type": "application/json"},
                          )
        assert r1.status_code == 400, r1.status_code
        expect_log.append({
            "level": "error",
            "message": "invalid query",
            "error": "service.name is required",
            "response_code": 400,
        })

        # no configuration for service
        r2 = requests.get(self.agent_config_url,
                          params={"service.name": service_name + "_cache_bust"},
                          headers={"Content-Type": "application/json"},
                          )
        assert r2.status_code == 200, r2.status_code
        expect_log.append({
            "level": "info",
            "message": "request ok",
            "response_code": 200,
        })
        self.assertDictEqual({}, r2.json())

        self.create_service_config({"transaction_sample_rate": 0.05}, service_name)

        # yes configuration for service
        r3 = requests.get(self.agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})
        assert r3.status_code == 200, r3.status_code
        # TODO (gr): validate Cache-Control header - https://github.com/elastic/apm-server/issues/2438
        expect_log.append({
            "level": "info",
            "message": "request ok",
            "response_code": 200,
        })
        self.assertDictEqual({"transaction_sample_rate": "0.05"}, r3.json())

        # not modified on re-request
        r3_again = requests.get(self.agent_config_url,
                                params={"service.name": service_name},
                                headers={
                                    "Content-Type": "application/json",
                                    "If-None-Match": r3.headers["Etag"],
                                })
        assert r3_again.status_code == 304, r3_again.status_code
        expect_log.append({
            "level": "info",
            "message": "not modified",
            "response_code": 304,
        })

        config_id = self.create_service_config(
            {"transaction_sample_rate": 0.15}, service_name, env=service_env)

        # yes configuration for service+environment
        r4 = requests.get(self.agent_config_url,
                          params={
                              "service.name": service_name,
                              "service.environment": service_env,
                          },
                          headers={"Content-Type": "application/json"})
        assert r4.status_code == 200, r4.status_code
        self.assertDictEqual({"transaction_sample_rate": "0.15"}, r4.json())
        expect_log.append({
            "level": "info",
            "message": "request ok",
            "response_code": 200,
        })

        # not modified on re-request
        r4_again = requests.get(self.agent_config_url,
                                params={
                                    "service.name": service_name,
                                    "service.environment": service_env,
                                },
                                headers={
                                    "Content-Type": "application/json",
                                    "If-None-Match": r4.headers["Etag"],
                                })
        assert r4_again.status_code == 304, r4_again.status_code
        expect_log.append({
            "level": "info",
            "message": "not modified",
            "response_code": 304,
        })

        self.update_service_config(
            config_id, {"transaction_sample_rate": 0.99}, service_name, env=service_env)

        # TODO (gr): remove when cache can be disabled via config
        # wait for cache to purge
        time.sleep(1.1)  # sleep much more than acm_cache_expiration to reduce flakiness

        r4_post_update = requests.get(self.agent_config_url,
                                      params={
                                          "service.name": service_name,
                                          "service.environment": service_env,
                                      },
                                      headers={
                                          "Content-Type": "application/json",
                                          "If-None-Match": r4.headers["Etag"],
                                      })
        assert r4_post_update.status_code == 200, r4_post_update.status_code
        self.assertDictEqual({"transaction_sample_rate": "0.99"}, r4_post_update.json())
        expect_log.append({
            "level": "info",
            "message": "request ok",
            "response_code": 200,
        })

        # configuration for service+environment (all includes non existing)
        r5 = requests.get(self.agent_config_url,
                          params={
                              "service.name": service_name,
                              "service.environment": bad_service_env,
                          },
                          headers={"Content-Type": "application/json"})
        assert r5.status_code == 200, r5.status_code
        expect_log.append({
            "level": "info",
            "message": "request ok",
            "response_code": 200,
        })
        self.assertDictEqual({"transaction_sample_rate": "0.05"}, r5.json())

        config_request_logs = list(self.logged_requests(url="/config/v1/agents"))
        assert len(config_request_logs) == len(expect_log)
        for want, got in zip(expect_log, config_request_logs):
            self.assertDictContainsSubset(want, got)

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_rum_disabled(self):
        r = requests.get(self.rum_agent_config_url,
                         params={
                             "service.name": "rum-service",
                         },
                         headers={"Content-Type": "application/json"}
                         )
        assert r.status_code == 403
        assert r.json() == {'error': 'forbidden request: endpoint is disabled'}


class AgentConfigurationKibanaDownIntegrationTest(ElasticTest):
    config_overrides = {
        "logging_json": "true",
        "secret_token": "supersecret",
        "kibana_enabled": "true",
        "kibana_host": "unreachablehost"
    }

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_config_requests(self):
        r1 = requests.get(self.agent_config_url,
                          headers={
                              "Content-Type": "application/json",
                          })
        assert r1.status_code == 401, r1.status_code

        r2 = requests.get(self.agent_config_url,
                          params={"service.name": "foo"},
                          headers={
                              "Content-Type": "application/json",
                              "Authorization": "Bearer " + self.config_overrides["secret_token"],
                          })
        assert r2.status_code == 503, r2.status_code

        config_request_logs = list(self.logged_requests(url="/config/v1/agents"))
        assert len(config_request_logs) == 2, config_request_logs
        self.assertDictContainsSubset({
            "level": "error",
            "message": "invalid token",
            "error": "invalid token",
            "response_code": 401,
        }, config_request_logs[0])
        self.assertDictContainsSubset({
            "level": "error",
            "message": "unable to retrieve connection to Kibana",
            "response_code": 503,
        }, config_request_logs[1])


class AgentConfigurationKibanaDisabledIntegrationTest(ElasticTest):
    config_overrides = {
        "logging_json": "true",
        "kibana_enabled": "false",
    }

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_log_kill_switch_active(self):
        r = requests.get(self.agent_config_url,
                         headers={
                             "Content-Type": "application/json",
                         })
        assert r.status_code == 403, r.status_code
        config_request_logs = list(self.logged_requests(url="/config/v1/agents"))
        self.assertDictContainsSubset({
            "level": "error",
            "message": "forbidden request",
            "error": "forbidden request: endpoint is disabled",
            "response_code": 403,
        }, config_request_logs[0])


class RumAgentConfigurationIntegrationTest(AgentConfigurationTest):
    config_overrides = {
        "enable_rum": "true",
    }

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_rum(self):
        service_name = "rum-service"
        self.create_service_config({"transaction_sample_rate": 0.2}, service_name, agent="rum-js")

        r1 = requests.get(self.rum_agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r1.status_code == 200
        assert r1.json() == {'transaction_sample_rate': '0.2'}
        etag = r1.headers["Etag"].replace('"', '')  # RUM will send it without double quotes

        r2 = requests.get(self.rum_agent_config_url,
                          params={"service.name": service_name, "ifnonematch": etag},
                          headers={"Content-Type": "application/json"})
        assert r2.status_code == 304

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_backend_after_rum(self):
        service_name = "backend-service"
        self.create_service_config({"transaction_sample_rate": 0.3}, service_name)

        r1 = requests.get(self.rum_agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r1.status_code == 200, r1.status_code
        assert r1.json() == {}, r1.json()

        r2 = requests.get(self.agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r2.status_code == 200, r2.status_code
        assert r2.json() == {"transaction_sample_rate": "0.3"}, r2.json()

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_rum_after_backend(self):
        service_name = "backend-service"
        self.create_service_config({"transaction_sample_rate": 0.3}, service_name)

        r1 = requests.get(self.agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r1.status_code == 200, r1.status_code
        assert r1.json() == {"transaction_sample_rate": "0.3"}, r1.json()

        r2 = requests.get(self.rum_agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r2.status_code == 200, r2.status_code
        assert r2.json() == {}, r2.json()

    @unittest.skipUnless(INTEGRATION_TESTS, "integration test")
    def test_all_agents(self):
        service_name = "any-service"
        self.create_service_config(
            {"transaction_sample_rate": 0.4, "capture_body": "all"}, service_name, agent="")

        r1 = requests.get(self.rum_agent_config_url,
                          params={"service.name": service_name},
                          headers={"Content-Type": "application/json"})

        assert r1.status_code == 200, r1.status_code
        # only return settings applicable to RUM
        assert r1.json() == {"transaction_sample_rate": "0.4"}, r1.json()
