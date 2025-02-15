import json
import os
from datetime import date, timedelta
from xml.etree import ElementTree

import requests
import responses
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.signing import dumps
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from payments.data import SUPPORTED_LANGUAGES
from payments.models import Customer, Payment

from .management.commands.recurring_payments import Command as RecurringPaymentsCommand
from .models import PAYMENTS_ORIGIN, Donation, Package, Post, Service
from .remote import (
    ACTIVITY_URL,
    WEBLATE_CONTRIBUTORS_URL,
    get_activity,
    get_contributors,
)
from .templatetags.downloads import downloadlink, filesizeformat

TEST_DATA = os.path.join(os.path.dirname(__file__), "test-data")
TEST_FAKTURACE = os.path.join(TEST_DATA, "fakturace")
TEST_CONTRIBUTORS = os.path.join(TEST_DATA, "contributors.json")
TEST_ACTIVITY = os.path.join(TEST_DATA, "activity.json")
TEST_IMAGE = os.path.join(TEST_DATA, "weblate-html.png")

TEST_CUSTOMER = {
    "name": "Michal Čihař",
    "address": "Zdiměřická 1439",
    "city": "149 00 Praha 4",
    "country": "CZ",
    "vat_0": "CZ",
    "vat_1": "8003280318",
}


def fake_remote():
    cache.set(
        "VAT-CZ8003280318",
        {
            "countryCode": "CZ",
            "vatNumber": "8003280318",
            "requestDate": date(2020, 3, 20),
            "valid": True,
            "name": "Ing. Michal Čihař",
            "address": "Zdiměřická 1439/8\nPRAHA 11 - CHODOV\n149 00  PRAHA 415",
        },
    )
    cache.set("wlweb-contributors", [])
    cache.set("wlweb-activity-stats", [])
    cache.set(
        "wlweb-changes-list",
        [
            {
                "failing_percent": 0.4,
                "translated_percent": 20.3,
                "total_words": 12385202,
                "failing": 3708,
                "translated_words": 1773069,
                "url_translate": "https://hosted.weblate.org/projects/godot-engine/",
                "fuzzy_percent": 3.9,
                "recent_changes": 2401,
                "translated": 160302,
                "fuzzy": 31342,
                "total": 787070,
                "last_change": timezone.now(),
                "name": "Godot Engine",
                "url": "https://hosted.weblate.org/engage/godot-engine/",
            },
            {
                "failing_percent": 1.4,
                "translated_percent": 46.0,
                "total_words": 7482588,
                "failing": 14319,
                "translated_words": 2917305,
                "url_translate": "https://hosted.weblate.org/projects/phpmyadmin/",
                "fuzzy_percent": 12.3,
                "recent_changes": 3652,
                "translated": 465082,
                "fuzzy": 124794,
                "total": 1009956,
                "last_change": timezone.now() - timedelta(seconds=3600),
                "name": "phpMyAdmin",
                "url": "https://hosted.weblate.org/engage/phpmyadmin/",
            },
            {
                "failing_percent": 0.4,
                "translated_percent": 48.8,
                "total_words": 1386375,
                "failing": 1121,
                "translated_words": 586192,
                "url_translate": "https://hosted.weblate.org/projects/weblate/",
                "fuzzy_percent": 9.5,
                "recent_changes": 2864,
                "translated": 125298,
                "fuzzy": 24461,
                "total": 256275,
                "last_change": timezone.now() - timedelta(seconds=14400),
                "name": "Weblate",
                "url": "https://hosted.weblate.org/engage/weblate/",
            },
            {
                "failing_percent": 0.8,
                "translated_percent": 20.1,
                "total_words": 7707495,
                "failing": 4459,
                "translated_words": 1066440,
                "url_translate": "https://hosted.weblate.org/projects/f-droid/",
                "fuzzy_percent": 0.7,
                "recent_changes": 7080,
                "translated": 104941,
                "fuzzy": 4011,
                "total": 520867,
                "last_change": timezone.now() - timedelta(days=1),
                "name": "F-Droid",
                "url": "https://hosted.weblate.org/engage/f-droid/",
            },
            {
                "failing_percent": 1.0,
                "translated_percent": 72.9,
                "total_words": 324480,
                "failing": 883,
                "translated_words": 231003,
                "url_translate": "https://hosted.weblate.org/projects/freeplane/",
                "fuzzy_percent": 2.1,
                "recent_changes": 535,
                "translated": 61980,
                "fuzzy": 1787,
                "total": 84920,
                "last_change": timezone.now() - timedelta(days=4),
                "name": "Freeplane",
                "url": "https://hosted.weblate.org/engage/freeplane/",
            },
            {
                "failing_percent": 4.4,
                "translated_percent": 57.8,
                "total_words": 2016830,
                "failing": 25211,
                "translated_words": 1036249,
                "url_translate": "https://hosted.weblate.org/projects/osmand/",
                "fuzzy_percent": 3.0,
                "recent_changes": 3633,
                "translated": 325725,
                "fuzzy": 16981,
                "total": 562798,
                "last_change": timezone.now() - timedelta(days=30),
                "name": "OsmAnd",
                "url": "https://hosted.weblate.org/engage/osmand/",
            },
        ],
    )


def fake_payment(url):
    response = requests.get(url, timeout=1)
    response = requests.get(response.url[:-1] + "p", timeout=1)
    body = response.content.decode()
    for line in body.splitlines():
        if '<input type="hidden" name="id"' in line:
            payment_number = line.split('value="')[1].split('"')[0]
    # Confirm payment state
    response = requests.post(
        "https://www.thepay.cz/demo-gate/return.php",
        data={"state": 2, "underpaid_value": 1, "id": payment_number},
        allow_redirects=False,
        timeout=1,
    )
    return response.headers["Location"]


class PostTestCase(TestCase):
    @staticmethod
    def create_post(title="testpost", body="testbody", timestamp=None):
        if timestamp is None:
            timestamp = timezone.now() - relativedelta(days=1)
        return Post.objects.create(
            title=title, slug=title, body=body, timestamp=timestamp
        )


class ViewTestCase(PostTestCase):
    """Views testing."""

    def setUp(self):
        super().setUp()
        fake_remote()

    def test_index_redirect(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/en/", 302)

    def test_index_en(self):
        response = self.client.get("/en/")
        self.assertContains(response, "yearly")

    def test_index_cs(self):
        response = self.client.get("/cs/")
        self.assertContains(response, "ročně")

    def test_index_he(self):
        response = self.client.get("/he/")
        self.assertContains(response, "שנתי")

    def test_index_be(self):
        response = self.client.get("/be/")
        self.assertContains(response, "штогод")

    def test_index_be_latin(self):
        response = self.client.get("/be-latn/")
        self.assertContains(response, "Nieabmiežavany")

    def test_terms(self):
        response = self.client.get("/en/terms/")
        self.assertContains(response, "04705904")

    def test_security_txt(self):
        response = self.client.get("/security.txt", follow=True)
        self.assertRedirects(response, "/.well-known/security.txt", status_code=301)
        self.assertContains(response, "https://hackerone.com/weblate")

    def test_localized_docs(self):
        response = self.client.get("/uk/contribute/")
        self.assertContains(response, "https://docs.weblate.org/uk/latest/contributing")

    @responses.activate
    def test_about(self):
        with open(TEST_CONTRIBUTORS) as handle:
            responses.add(responses.GET, WEBLATE_CONTRIBUTORS_URL, body=handle.read())
        get_contributors(force=True)
        response = self.client.get("/en/about/")
        self.assertContains(response, "comradekingu")
        # Test error handling, cached content should stay there
        responses.replace(responses.GET, WEBLATE_CONTRIBUTORS_URL, status=500)
        get_contributors(force=True)
        response = self.client.get("/en/about/")
        self.assertContains(response, "comradekingu")

    @responses.activate
    def test_activity(self):
        with open(TEST_ACTIVITY) as handle:
            responses.add(responses.GET, ACTIVITY_URL, body=handle.read())
        get_activity(force=True)
        response = self.client.get("/img/activity.svg")
        self.assertContains(response, "<svg")
        # Test error handling, cached content should stay there
        responses.replace(responses.GET, ACTIVITY_URL, status=500)
        get_activity(force=True)
        response = self.client.get("/img/activity.svg")
        self.assertContains(response, "<svg")

    def test_download_en(self):
        response = self.client.get("/en/download/")
        self.assertContains(response, "Download Weblate")

    def test_sitemap_lang(self):
        response = self.client.get("/sitemap-es.xml")
        self.assertContains(response, "http://testserver/es/features/")

    def test_sitemap_news(self):
        self.create_post()
        response = self.client.get("/sitemap-news.xml")
        self.assertContains(response, "testpost")

    def test_sitemaps(self):
        # Get root sitemap
        response = self.client.get("/sitemap.xml")
        self.assertContains(response, "<sitemapindex")

        # Parse it
        tree = ElementTree.fromstring(response.content)  # noqa: S314
        sitemaps = tree.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap")
        for sitemap in sitemaps:
            location = sitemap.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            response = self.client.get(location.text)
            self.assertContains(response, "<urlset")
            # Try if it's a valid XML
            ElementTree.fromstring(response.content)  # noqa: S314


class UtilTestCase(TestCase):
    """Helper code testing."""

    def test_format(self):
        self.assertEqual(filesizeformat(0), "0 bytes")
        self.assertEqual(filesizeformat(1000), "1000 bytes")
        self.assertEqual(filesizeformat(1000000), "976.6 KiB")
        self.assertEqual(filesizeformat(1000000000), "953.7 MiB")
        self.assertEqual(filesizeformat(10000000000000), "9313.2 GiB")

    def test_downloadlink(self):
        self.assertEqual(
            downloadlink(
                {
                    "comment_text": "",
                    "digests": {
                        "blake2b_256": "67b8258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a",
                        "md5": "e4acec80cbda61a4dffbc591062c1e0e",
                        "sha256": "59224c80144b7784b6efb6dae6bc17745cbbb7938c417c436237d695d75a7db2",
                    },
                    "downloads": -1,
                    "filename": "Weblate-5.3.1-py3-none-any.whl",
                    "has_sig": False,
                    "md5_digest": "e4acec80cbda61a4dffbc591062c1e0e",
                    "packagetype": "bdist_wheel",
                    "python_version": "py3",
                    "requires_python": ">=3.9",
                    "size": 68485094,
                    "upload_time": "2023-12-19T14:02:25",
                    "upload_time_iso_8601": "2023-12-19T14:02:25.035680Z",
                    "url": "https://files.pythonhosted.org/packages/67/b8/258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a/Weblate-5.3.1-py3-none-any.whl",
                    "yanked": False,
                    "yanked_reason": None,
                }
            ),
            {
                "name": "Weblate-5.3.1-py3-none-any.whl",
                "size": "65.3 MiB",
                "text": "Wheel package",
                "url": "https://files.pythonhosted.org/packages/67/b8/258109f5829a8a616552cee382ed827c606bf397f992b068e744c533d86a/Weblate-5.3.1-py3-none-any.whl",
            },
        )
        self.assertEqual(
            downloadlink(
                {
                    "comment_text": "",
                    "digests": {
                        "blake2b_256": "149cb3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857",
                        "md5": "b0cb1a21719712693ea85e4c14ba8559",
                        "sha256": "0b3a3862b3703efee302b62d28914e5b610405eec9db1c232729b31f41a694e5",
                    },
                    "downloads": -1,
                    "filename": "Weblate-5.3.1.tar.gz",
                    "has_sig": False,
                    "md5_digest": "b0cb1a21719712693ea85e4c14ba8559",
                    "packagetype": "sdist",
                    "python_version": "source",
                    "requires_python": ">=3.9",
                    "size": 69811080,
                    "upload_time": "2023-12-19T14:02:53",
                    "upload_time_iso_8601": "2023-12-19T14:02:53.564201Z",
                    "url": "https://files.pythonhosted.org/packages/14/9c/b3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857/Weblate-5.3.1.tar.gz",
                    "yanked": False,
                    "yanked_reason": None,
                }
            ),
            {
                "name": "Weblate-5.3.1.tar.gz",
                "size": "66.6 MiB",
                "text": "Sources tarball, gzip compressed",
                "url": "https://files.pythonhosted.org/packages/14/9c/b3501dc08c06d1c3f9f9b71746268731b7b25e9b13122679d8a219b74857/Weblate-5.3.1.tar.gz",
            },
        )


class FakturaceTestCase(TestCase):
    databases = "__all__"
    credentials = {
        "username": "testuser",
        "password": "testpassword",
        "email": "noreply@weblate.org",
    }
    _user = None

    def login(self):
        user = self.create_user()
        self.client.login(**self.credentials)
        return user

    @staticmethod
    def create_payment(recurring="y"):
        customer = Customer.objects.create(
            email="weblate@example.com",
            user_id=1,
            origin=PAYMENTS_ORIGIN,
        )
        payment = Payment.objects.create(
            customer=customer,
            amount=100,
            description="Test payment",
            backend="pay",
            recurring=recurring,
        )
        return (
            payment,
            reverse("payment", kwargs={"pk": payment.pk}),
            reverse("payment-customer", kwargs={"pk": payment.pk}),
        )

    def create_user(self):
        if self._user is None:
            self._user = User.objects.create_user(**self.credentials)
        return self._user

    def create_donation(self, years=1, days=0, recurring="y"):
        return Donation.objects.create(
            reward=3,
            user=self.create_user(),
            active=True,
            expires=timezone.now() + relativedelta(years=years, days=days),
            payment=self.create_payment(recurring=recurring)[0].pk,
            link_url="https://example.com/weblate",
            link_text="Weblate donation test",
        )

    def create_service(self, years=1, days=0, recurring="y", package="extended"):
        Package.objects.bulk_create(
            [
                Package(name="community", verbose="Community support", price=0),
                Package(name="extended", verbose="Extended support", price=42),
                Package(name="hosted:test-1", verbose="Hosted (basic)", price=420),
                Package(name="hosted:test-2", verbose="Hosted (upgraded)", price=840),
            ]
        )
        service = Service.objects.create()
        service.subscription_set.create(
            package=package,
            expires=timezone.now() + relativedelta(years=years, days=days),
            payment=self.create_payment(recurring=recurring)[0].pk,
        )
        service.users.add(self.create_user())
        return service


class PaymentsTest(FakturaceTestCase):
    def setUp(self):
        super().setUp()
        fake_remote()

    def test_languages(self):
        self.assertEqual(
            set(SUPPORTED_LANGUAGES),
            {x[0] for x in settings.LANGUAGES},
        )

    def test_view(self):
        with override("en"):
            payment, url, customer_url = self.create_payment()
            response = self.client.get(url, follow=True)
            self.assertRedirects(response, customer_url)
            self.assertContains(response, "Please provide your billing")
            response = self.client.post(
                customer_url,
                TEST_CUSTOMER,
                follow=True,
            )
            self.assertRedirects(response, url)
            self.assertContains(response, "Test payment")
            self.assertContains(response, "€121.0")
            return payment, url, customer_url

    def check_payment(self, payment, state):
        fresh = Payment.objects.get(pk=payment.pk)
        self.assertEqual(fresh.state, state)

    @override_settings(PAYMENT_DEBUG=True, PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_pay(self):
        payment, url, _dummy = self.test_view()
        response = self.client.post(url, {"method": "pay"})
        self.assertRedirects(
            response,
            f"{PAYMENTS_ORIGIN}?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.ACCEPTED)

    @override_settings(PAYMENT_DEBUG=True, PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_invalid_vat(self):
        payment, url, customer_url = self.test_view()
        # Inject invalid VAT
        customer = Customer.objects.get(pk=payment.customer.pk)
        customer.vat = "CZ8003280317"
        customer.save()

        response = self.client.get(url, follow=True)
        self.assertRedirects(response, customer_url)
        self.assertContains(response, "The VAT ID is no longer valid")

    @override_settings(PAYMENT_DEBUG=True)
    def test_reject(self):
        payment, url, _dummy = self.test_view()
        response = self.client.post(url, {"method": "reject"})
        self.assertRedirects(
            response,
            f"{PAYMENTS_ORIGIN}?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.REJECTED)

    @override_settings(PAYMENT_DEBUG=True, PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_pending(self):
        payment, url, _dummy = self.test_view()
        response = self.client.post(url, {"method": "pending"})
        complete_url = reverse("payment-complete", kwargs={"pk": payment.pk})
        self.assertRedirects(
            response,
            "https://cihar.com/?url=http://testserver" + complete_url,
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.PENDING)
        response = self.client.get(complete_url)
        self.assertRedirects(
            response,
            f"{PAYMENTS_ORIGIN}?payment={payment.pk}",
            fetch_redirect_response=False,
        )
        self.check_payment(payment, Payment.ACCEPTED)


class DonationTest(FakturaceTestCase):
    def setUp(self):
        super().setUp()
        fake_remote()

    def test_donate_page(self):
        response = self.client.get("/en/donate/")
        self.assertContains(response, "/donate/new/")
        self.login()

        # Check rewards on page
        response = self.client.get("/en/donate/new/")
        self.assertContains(response, "list of supporters")

    @override_settings(PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_service_workflow_card(self):
        self.login()
        Package.objects.create(name="community", verbose="Community support", price=0)
        Package.objects.create(name="extended", verbose="Extended support", price=42)
        response = self.client.get("/en/subscription/new/?plan=extended", follow=True)
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "thepay-card"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://www.thepay.cz/demo-gate/"))

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

        # Perform the payment
        complete_url = fake_payment(response.url)

        # Back to our web
        response = self.client.get(complete_url, follow=True)
        self.assertRedirects(response, "/en/user/")
        self.assertContains(response, "Thank you for your subscription")

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PROCESSED)

    def test_donation_workflow_invalid_reward(self):
        self.login()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 10, "reward": 2},
            follow=True,
        )
        self.assertContains(response, "Insufficient donation for selected reward!")

    def test_donation_workflow_card_reward(self):
        self.test_donation_workflow_card(2)

    @override_settings(PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_donation_workflow_card(self, reward=0):
        self.login()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 1000, "reward": reward},
            follow=True,
        )
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "thepay-card"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://www.thepay.cz/demo-gate/"))

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

        # Perform the payment
        complete_url = fake_payment(response.url)

        # Back to our web
        response = self.client.get(complete_url, follow=True)
        donation = Donation.objects.all().get()
        redirect_url = f"/en/donate/edit/{donation.pk}/" if reward else "/en/user/"
        self.assertRedirects(response, redirect_url)
        self.assertContains(response, "Thank you for your donation")

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PROCESSED)

        # Manual renew
        response = self.client.post(
            reverse("donate-pay", kwargs={"pk": donation.pk}), follow=True
        )
        renew = Payment.objects.exclude(pk=payment.pk).get()
        self.assertEqual(renew.state, Payment.NEW)
        self.assertContains(response, "Please choose payment method")

        response = self.client.post(
            reverse("payment", kwargs={"pk": renew.uuid}), {"method": "thepay-card"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://www.thepay.cz/demo-gate/"))

        renew.refresh_from_db()
        self.assertEqual(renew.state, Payment.PENDING)

        # Perform the payment
        complete_url = fake_payment(response.url)

        # Back to our web
        response = self.client.get(complete_url, follow=True)
        self.assertRedirects(response, redirect_url)
        self.assertContains(response, "Thank you for your donation")

        renew.refresh_from_db()
        self.assertEqual(renew.state, Payment.PROCESSED)

    @override_settings(PAYMENT_FAKTURACE=TEST_FAKTURACE)
    def test_donation_workflow_bank(self):
        self.login()
        response = self.client.post(
            "/en/donate/new/",
            {"recurring": "y", "amount": 10, "reward": 0},
            follow=True,
        )
        self.assertContains(response, "Please provide your billing")
        payment = Payment.objects.all().get()
        self.assertEqual(payment.state, Payment.NEW)
        customer_url = reverse("payment-customer", kwargs={"pk": payment.uuid})
        payment_url = reverse("payment", kwargs={"pk": payment.uuid})
        self.assertRedirects(response, customer_url)
        response = self.client.post(customer_url, TEST_CUSTOMER, follow=True)
        self.assertContains(response, "Please choose payment method")
        response = self.client.post(payment_url, {"method": "fio-bank"}, follow=True)
        self.assertContains(response, "Payment Instructions")

        payment.refresh_from_db()
        self.assertEqual(payment.state, Payment.PENDING)

    def test_your_donations(self):
        # Check login link
        self.assertContains(self.client.get(reverse("donate")), "/saml2/login/")
        user = self.login()

        # No login/donations
        response = self.client.get(reverse("donate"))
        self.assertNotContains(response, "/saml2/login/")
        self.assertNotContains(response, "Your donations")

        # Donation show show up
        Donation.objects.create(
            reward=2,
            user=user,
            active=True,
            expires=timezone.now() + relativedelta(years=1),
            payment=self.create_payment()[0].pk,
        )
        self.assertContains(self.client.get(reverse("user")), "My donations")

    def test_link(self):
        self.create_donation()
        response = self.client.get("/en/thanks/", follow=True)
        self.assertContains(response, "https://example.com/weblate")
        self.assertContains(response, "Weblate donation test")

    @responses.activate
    @override_settings(
        PAYMENT_DEBUG=True,
        PAYMENT_REDIRECT_URL="http://example.com/payment",
    )
    def test_recurring(self):
        responses.add(
            responses.POST,
            "http://example.com/payment",
            body="",
        )
        donation = self.create_donation(-1)
        self.assertEqual(donation.payment_obj.payment_set.count(), 0)
        # The processing fails here, but new payment is created
        call_command("recurring_payments")
        self.assertEqual(donation.payment_obj.payment_set.count(), 1)
        # Flag it as paid
        donation.payment_obj.payment_set.update(state=Payment.ACCEPTED)

        # Process pending payments
        call_command("process_payments")
        old = donation.expires
        donation.refresh_from_db()
        self.assertGreater(donation.expires, old)


class PostTest(PostTestCase):
    def setUp(self):
        super().setUp()
        fake_remote()

    def test_future(self):
        past = self.create_post()
        future = self.create_post(
            "futurepost", "futurebody", timezone.now() + relativedelta(days=1)
        )
        response = self.client.get("/feed/")
        self.assertContains(response, "testpost")
        self.assertNotContains(response, "futurepost")
        response = self.client.get("/news/", follow=True)
        self.assertContains(response, "testpost")
        self.assertNotContains(response, "futurepost")
        response = self.client.get(past.get_absolute_url(), follow=True)
        self.assertContains(response, "testpost")
        self.assertContains(response, "testbody")
        response = self.client.get(future.get_absolute_url(), follow=True)
        self.assertEqual(response.status_code, 404)


class APITest(TestCase):
    databases = "__all__"

    def test_hosted(self):
        Package.objects.create(name="community", verbose="Community support", price=0)
        Package.objects.create(name="shared:test", verbose="Test package", price=0)
        response = self.client.post(
            "/api/hosted/",
            {
                "payload": dumps(
                    {
                        "billing": 42,
                        "package": "shared:test",
                        "projects": 1,
                        "languages": 1,
                        "source_strings": 1,
                        "words": 10,
                        "components": 1,
                        "users": [666],
                    },
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.hosted",
                )
            },
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        self.assertEqual(response.status_code, 200)

    def test_hosted_invalid(self):
        response = self.client.post("/api/hosted/", {"payload": dumps({}, key="dummy")})
        self.assertEqual(response.status_code, 400)

    def test_hosted_missing(self):
        response = self.client.post("/api/hosted/")
        self.assertEqual(response.status_code, 400)

    def test_support_missing(self):
        response = self.client.post("/api/support/")
        self.assertEqual(response.status_code, 404)

    def test_support(self, delta=1, expected="extended"):
        Package.objects.create(name="community", verbose="Community support", price=0)
        Package.objects.create(name="extended", verbose="Extended support", price=42)
        service = Service.objects.create()
        service.subscription_set.create(
            package="extended", expires=timezone.now() + timedelta(days=delta)
        )
        response = self.client.post(
            "/api/support/",
            {"secret": service.secret},
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], expected)
        return service

    def test_support_expired(self):
        self.test_support(delta=-1, expected="community")

    def test_support_discovery(self):
        service = self.test_support()
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)
        self.client.post(
            "/api/support/",
            {"secret": service.secret, "discoverable": "1"},
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        service = Service.objects.get(pk=service.pk)
        self.assertTrue(service.discoverable)
        self.client.post(
            "/api/support/",
            {"secret": service.secret},
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)

    def test_support_discovery_projects(self):
        service = self.test_support()
        service = Service.objects.get(pk=service.pk)
        self.assertFalse(service.discoverable)
        self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "discoverable": "1",
                "public_projects": json.dumps(
                    [
                        {
                            "name": "Prj1",
                            "url": "/projects/p/",
                            "web": "https://weblate.org/",
                        }
                    ]
                ),
            },
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        self.assertEqual(service.project_set.count(), 1)
        project = service.project_set.get()
        self.assertEqual(project.name, "Prj1")
        self.client.post(
            "/api/support/",
            {
                "secret": service.secret,
                "discoverable": "1",
                "public_projects": json.dumps(
                    [
                        {
                            "name": "Prj2",
                            "url": "/projects/p/",
                            "web": "https://weblate.org/",
                        }
                    ]
                ),
            },
            HTTP_USER_AGENT="weblate/1.2.3",
        )
        self.assertEqual(service.project_set.count(), 1)
        project = service.project_set.get()
        self.assertEqual(project.name, "Prj2")

    def test_user(self):
        user = User.objects.create(
            username="testuser",
            password="testpassword",  # noqa: S106
        )
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {"username": user.username},
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "User updated"})
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {
                        "username": "x",
                        "create": {
                            "username": "x",
                            "last_name": "First Last",
                            "email": "noreply@weblate.org",
                        },
                    },
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "User created"})

    def test_user_invalid(self):
        response = self.client.post("/api/user/", {"payload": dumps({}, key="dummy")})
        self.assertEqual(response.status_code, 400)

    def test_user_missing(self):
        response = self.client.post("/api/user/")
        self.assertEqual(response.status_code, 400)

    def test_user_rename(self):
        user = User.objects.create(
            username="testuser",
            password="testpassword",  # noqa: S106
        )
        response = self.client.post(
            "/api/user/",
            {
                "payload": dumps(
                    {"username": user.username, "changes": {"username": "other"}},
                    key=settings.PAYMENT_SECRET,
                    salt="weblate.user",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="testuser").exists())
        self.assertTrue(User.objects.filter(username="other").exists())


@override_settings(
    NOTIFY_SUBSCRIPTION=["noreply@example.com"],
    PAYMENT_DEBUG=True,
    PAYMENT_FAKTURACE=TEST_FAKTURACE,
    PAYMENT_REDIRECT_URL="http://example.com/payment",
)
class ExpiryTest(FakturaceTestCase):
    def assert_notifications(self, *subjects):
        self.assertEqual({m.subject for m in mail.outbox}, set(subjects))
        mail.outbox = []

    def test_expiring_donate(self):
        self.create_donation(years=0, days=3, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_expiring_recurring_donate(self):
        self.create_donation(years=0, days=3)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications()
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, settings.PAYMENT_REDIRECT_URL, body="")
            RecurringPaymentsCommand.handle_donations()
        self.assert_notifications()
        RecurringPaymentsCommand.handle_donations()
        self.assert_notifications()

    def test_expiring_donate_notify_user(self):
        self.create_donation(years=0, days=8, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming renewal on weblate.org",
        )

    def test_expiring_recurring_donate_notify_user(self):
        self.create_donation(years=0, days=8)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Your upcoming payment on weblate.org")

    def test_expiring_subscription(self):
        self.create_service(years=0, days=3, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Expiring subscriptions on weblate.org")
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications("Your expired payment on weblate.org")

    def test_expiring_recurring_subscription(self):
        self.create_service(years=0, days=3)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications()
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, settings.PAYMENT_REDIRECT_URL, body="")
            RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications()
        RecurringPaymentsCommand.handle_subscriptions()
        self.assert_notifications()

    def test_expiring_subscription_notify_user(self):
        self.create_service(years=0, days=8, recurring="")
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications(
            "Expiring subscriptions on weblate.org",
            "Your upcoming renewal on weblate.org",
        )

    def test_expiring_recurring_subscription_notify_user(self):
        self.create_service(years=0, days=8)
        RecurringPaymentsCommand.notify_expiry()
        self.assert_notifications("Your upcoming payment on weblate.org")


@override_settings(
    NOTIFY_SUBSCRIPTION=["noreply@example.com"],
    PAYMENT_DEBUG=True,
    PAYMENT_FAKTURACE=TEST_FAKTURACE,
)
class ServiceTest(FakturaceTestCase):
    def test_hosted_upgrade(self):
        with override("en"):
            self.login()
            service = self.create_service(
                years=0, days=3, recurring="", package="hosted:test-1"
            )
            suggestions = service.get_suggestions()
            self.assertEqual(len(suggestions), 1)
            self.assertEqual(suggestions[0][0], "hosted:test-2")
            response = self.client.get(
                reverse("subscription-new"),
                {"plan": "hosted:test-2", "service": service.pk},
                follow=True,
            )
            payment_url = response.redirect_chain[0][0].split("localhost:1234")[-1]
            payment_edit_url = response.redirect_chain[1][0]
            self.assertTrue(payment_url.startswith("/en/payment/"))
            response = self.client.post(payment_edit_url, TEST_CUSTOMER, follow=True)
            self.assertRedirects(response, payment_url)
            response = self.client.post(payment_url, {"method": "pay"}, follow=True)
            self.assertRedirects(response, reverse("user"))
            self.assertContains(response, "Weblate: Hosted (upgraded)")

        service = Service.objects.get(pk=service.pk)
        hosted = service.hosted_subscriptions
        self.assertEqual(len(hosted), 1)
        self.assertEqual(hosted[0].package, "hosted:test-2")
