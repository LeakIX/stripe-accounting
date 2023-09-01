from abc import ABCMeta, abstractmethod
import json
import requests
from decimal import Decimal
import datetime
from typing import List


class AbstractReport(metaclass=ABCMeta):
    @abstractmethod
    def make(self) -> List[str]:
        pass


class SubscriptionCanceledReport(AbstractReport):
    def __init__(self, customer_email: str, datetime: datetime.datetime):
        self.customer_email = customer_email
        self.datetime = datetime

    def make(self) -> List[str]:
        return [
            "Customer %s has canceled the subscription on %s"
            % (
                self.customer_email,
                self.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            )
        ]


class AbstractReportingPlatform(metaclass=ABCMeta):
    NAME = None

    @abstractmethod
    def post(self):
        pass


class Mattermost(AbstractReportingPlatform):
    NAME = "mattermost"
    CONFIGURATION_KEYS = [
        "MATTERMOST_URL",
    ]

    def __init__(self, *args, **kwargs):
        self.url = kwargs["MATTERMOST_URL"]

    def post(self, report: AbstractReport):
        lines = report.make()
        for line in lines:
            data = {"text": line}
            headers = {"Content-Type": "application/json"}
            response = requests.post(self.url, data=json.dumps(data), headers=headers)
            response.raise_for_status()


class Stdin(AbstractReportingPlatform):
    NAME = "stdin"
    CONFIGURATION_KEYS = []

    def post(self, report: AbstractReport):
        lines = report.make()
        for line in lines:
            print(line)


AVAILABLE_REPORTING_PLATFORMS = {v.NAME: v for v in [Mattermost, Stdin]}
