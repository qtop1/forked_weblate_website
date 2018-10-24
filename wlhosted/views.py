# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.translation import get_language, ugettext as _
from django.views.generic.edit import FormView

from weblate.billing.models import Plan, Billing
from weblate.utils import messages

from wlhosted.forms import ChooseBillingForm
from wlhosted.models import Payment
from wlhosted.utils import get_origin

# List of supported languages on weblate.org
SUPPORTED_LANGUAGES = frozenset((
    'ar', 'az', 'be', 'be@latin', 'bg', 'br', 'ca', 'cs', 'da', 'de', 'en',
    'el', 'en-gb', 'es', 'fi', 'fr', 'gl', 'he', 'hu', 'id', 'it', 'ja', 'ko',
    'nb', 'nl', 'pl', 'pt', 'pt-br', 'ru', 'sk', 'sl', 'sq', 'sr', 'sv', 'tr',
    'uk', 'zh-hans', 'zh-hant',
))


def get_trial_billing(user):
    """Get trial billing for user to be ugpraded.

    We intentionally ignore in case there is more of them (what
    should not happen) to avoid need for manual selection.
    """
    billings = Billing.objects.for_user(user).filter(
        state=Billing.STATE_TRIAL
    )
    if billings.count() == 1:
        return billings[0]
    return None


@method_decorator(login_required, name='dispatch')
class CreateBillingView(FormView):
    template_name = 'hosted/create.html'
    form_class = ChooseBillingForm

    def handle_payment(self, request):
        try:
            payment = Payment.objects.get(
                uuid=request.GET['payment'],
                customer__user_id=request.user.id,
                customer__origin=get_origin(),
                paid=True,
            )
        except Payment.DoesNotExist:
            messages.error(request, _('No matching payment found.'))
            return redirect('create-billing')

        # TODO: handle incoming payment confirmation
        #  - create/update billing
        return None

    def get(self, request, *args, **kwargs):
        if 'payment' in request.GET:
            return self.handle_payment(request)
        billing = get_trial_billing(request.user)
        if billing is not None:
            messages.info(
                request,
                _('Choose plan to use for your trial.')
            )
        return super(CreateBillingView, self).get(request, *args, **kwargs)

    def get_success_url(self, payment):
        language = get_language()
        if language not in SUPPORTED_LANGUAGES:
            language = 'en'
        return settings.PAYMENT_REDIRECT_URL.format(
            language=language,
            uuid=payment.uuid
        )

    def form_valid(self, form):
        payment = form.create_payment(self.request.user)
        return HttpResponseRedirect(self.get_success_url(payment))

    def get_context_data(self, **kwargs):
        kwargs = super(CreateBillingView, self).get_context_data(**kwargs)
        kwargs['plans'] = Plan.objects.filter(
            public=True, price__gt=0
        ).order_by('price')
        return kwargs
