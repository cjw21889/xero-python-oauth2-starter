# -*- coding: utf-8 -*-
import os
from functools import wraps
from io import BytesIO
from logging.config import dictConfig

from flask import Flask, url_for, render_template, session, redirect, json, send_file
from flask_oauthlib.contrib.client import OAuth, OAuth2Application
from flask_session import Session
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient, serialize
from xero_python.api_client.configuration import Configuration
from xero_python.api_client.oauth2 import OAuth2Token
from xero_python.exceptions import AccountingBadRequestException
from xero_python.identity import IdentityApi
from xero_python.utils import getvalue

import logging_settings
from utils import jsonify, serialize_model
import dateutil
import pandas as pd
import json

dictConfig(logging_settings.default_settings)

# configure main flask application
app = Flask(__name__)
app.config.from_object("default_settings")
app.config.from_pyfile("config.py", silent=True)
print(app.config['CLIENT_ID'])

if app.config["ENV"] != "production":
    # allow oauth2 loop to run over http (used for local testing only)
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# configure persistent session cache
Session(app)

# configure flask-oauthlib application
# TODO fetch config from https://identity.xero.com/.well-known/openid-configuration #1
oauth = OAuth(app)
xero = oauth.remote_app(
    name="xero",
    version="2",
    client_id=app.config["CLIENT_ID"],
    client_secret=app.config["CLIENT_SECRET"],
    endpoint_url="https://api.xero.com/",
    authorization_url="https://login.xero.com/identity/connect/authorize",
    access_token_url="https://identity.xero.com/connect/token",
    refresh_token_url="https://identity.xero.com/connect/token",
    scope="offline_access openid profile email accounting.transactions "
    "accounting.reports.read accounting.journals.read accounting.settings "
    "accounting.contacts accounting.attachments assets projects",
)  # type: OAuth2Application


# configure xero-python sdk client
api_client = ApiClient(
    Configuration(
        debug=app.config["DEBUG"],
        oauth2_token=OAuth2Token(
            client_id=app.config["CLIENT_ID"], client_secret=app.config["CLIENT_SECRET"]
        ),
    ),
    pool_threads=1,
)


# configure token persistence and exchange point between flask-oauthlib and xero-python
@xero.tokengetter
@api_client.oauth2_token_getter
def obtain_xero_oauth2_token():
    return session.get("token")


@xero.tokensaver
@api_client.oauth2_token_saver
def store_xero_oauth2_token(token):
    session["token"] = token
    session.modified = True


def xero_token_required(function):
    @wraps(function)
    def decorator(*args, **kwargs):
        xero_token = obtain_xero_oauth2_token()
        if not xero_token:
            return redirect(url_for("login", _external=True))

        return function(*args, **kwargs)

    return decorator


@app.route("/")
def index():
    xero_access = dict(obtain_xero_oauth2_token() or {})
    return render_template(
        "code.html",
        title="Home | oauth token",
        code=json.dumps(xero_access, sort_keys=True, indent=4),
    )


@app.route("/login")
def login():
    redirect_url = url_for("oauth_callback", _external=True)
    response = xero.authorize(callback_uri=redirect_url)
    return response


@app.route("/callback")
def oauth_callback():
    try:
        response = xero.authorized_response()
    except Exception as e:
        print(e)
        raise
    # todo validate state value
    if response is None or response.get("access_token") is None:
        return "Access denied: response=%s" % response
    store_xero_oauth2_token(response)
    return redirect(url_for("index", _external=True))


@app.route("/logout")
def logout():
    store_xero_oauth2_token(None)
    return redirect(url_for("index", _external=True))


@app.route("/export-token")
@xero_token_required
def export_token():
    token = obtain_xero_oauth2_token()
    buffer = BytesIO("token={!r}".format(token).encode("utf-8"))
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="x.python",
        as_attachment=True,
        attachment_filename="oauth2_token.py",
    )


@app.route("/refresh-token")
@xero_token_required
def refresh_token():
    xero_token = obtain_xero_oauth2_token()
    new_token = api_client.refresh_oauth2_token()
    return render_template(
        "code.html",
        title="Xero OAuth2 token",
        code=jsonify({"Old Token": xero_token, "New token": new_token}),
        sub_title="token refreshed",
    )


@app.route("/tenants")
@xero_token_required
def tenants():
    identity_api = IdentityApi(api_client)
    accounting_api = AccountingApi(api_client)

    available_tenants = []
    for connection in identity_api.get_connections():
        tenant = serialize(connection)
        if connection.tenant_type == "ORGANISATION":
            organisations = accounting_api.get_organisations(
                xero_tenant_id=connection.tenant_id
            )
            tenant["organisations"] = serialize(organisations)

        available_tenants.append(tenant)
    hotels=[]
    for i in available_tenants:
        org = i.get('organisations',{}).get('Organisations',[{}])[0]
        if not org.get('IsDemoCompany', True):
            hotels.append({'tenant_id': i.get('tenantId'),
                        'name': i.get('tenantName'),
                        'api_key': org.get('APIKey'),
                        'currency': org.get('BaseCurrency'),
                        'org_id': org.get('OrganisationID'),
                        'org_status': org.get('OrganisationStatus'),
            })
    hotel_df = pd.DataFrame(hotels)
    hotel_df.to_csv('tenants1.csv', index=False)

    return render_template(
        "code.html",
        title="Xero Tenants",
        code=hotel_df,
        # code = available_tenants[0]['organisations']['Organisations'][0].keys()
        # code=json.dumps(available_tenants, sort_keys=True, indent=4)
    )


def get_xero_tenant_id(hotel):
    token = obtain_xero_oauth2_token()
    if not token:
        return None

    identity_api = IdentityApi(api_client)
    for connection in identity_api.get_connections():
        if connection.tenant_type == "ORGANISATION" and connection.tenant_name == hotel:
            return connection.tenant_id


def get_hotels():
    token = obtain_xero_oauth2_token()
    if not token:
        return None

    identity_api = IdentityApi(api_client)
    return {connection.tenant_name: connection.tenant_id for connection in identity_api.get_connections()}


@app.route("/tracking")
@xero_token_required
def get_tracking_categories(hotel ,id):
    accounting_api = AccountingApi(api_client)
    params = {
        'xero_tenant_id': id,
        'where': 'Status=="ACTIVE"',
        'order': 'Name ASC',
        'include_archived': 'true'
    }

    try:
        api_response = accounting_api.get_tracking_categories(**params).to_dict()
        master = api_response['tracking_categories'][0]
        categories = {}
        categories[master['tracking_category_id']] = {trk['name']:trk['tracking_option_id'] for trk in master['options']}
    except AccountingBadRequestException as e:
        categories = {}
        print(
            "Exception when calling AccountingApi->getTrackingCategories: %s\n"
            % e)

    # with open(f'cats_{hotel}.json', 'w') as fp:
    #     json.dump(categories, fp, indent=4)

    return categories


@xero_token_required
def get_accounts(hotel, id):
    accounting_api = AccountingApi(api_client)
    params = {
        'xero_tenant_id': id,
        'where': 'Status=="ACTIVE"',
        'order': 'Name ASC',
    }

    try:
        api_response = serialize(accounting_api.get_accounts(**params))
    except AccountingBadRequestException as e:
        api_response = ''
        print(
            "Exception when calling AccountingApi->getTrackingCategories: %s\n"
            % e)

    accts_df = pd.DataFrame(api_response['Accounts'])
    # accts_df.to_csv(f'accounts_{hotel}.csv', index=False)

    return accts_df



@app.route("/net-income")
@xero_token_required
def get_net_income():
    hotels = pd.read_csv('tenants1.csv')
    accounting_api = AccountingApi(api_client)
    hotel_trans = []
    for _, hotel in hotels.iterrows():
        params = {
                'xero_tenant_id': hotel['tenant_id'],
                'from_date': dateutil.parser.parse("2021-12-01"),
                'to_date': dateutil.parser.parse("2021-12-31"),
                'timeframe': 'MONTH',
                'standard_layout': 'True',
                'payments_only': 'false',
            }

        # make the call
        try:
            api_response = accounting_api.get_report_profit_and_loss(**params)
        except AccountingBadRequestException as e:
            api_response = ''
            print(
                "Exception when calling AccountingApi->getReportProfitAndLoss: %s\n"
                % e)

        base = serialize(api_response)['Reports'][0]
        for row in base['Rows']:
            if subset := row.get('Rows'):
                for r in subset:
                    item = r.get('Cells',[{},{}])
                    line = item[0].get('Value')
                    value = item[1].get('Value')
                    if line == 'Net Income':
                        hotel_trans.append({
                                    # 'organization_name': hotel['name'],
                                    'org_value': value,
                                    # 'line': line
                                })
    net_income_df = pd.DataFrame(hotel_trans)
    net_income_df.to_csv('NI.csv', index=False)


    return render_template("code.html",
                        title="Net Income",
                        # sub_title=report,
                        code = net_income_df)
                        # code=json.dumps(base, indent=4))

@app.route("/p-and-l")
@xero_token_required
def get_p_and_l():
    # create API instance
    accounting_api = AccountingApi(api_client)

    # run tenants function to get app connections
    # tenants()
    hotels = pd.read_csv('tenants.csv')

    all_hotels_df = pd.DataFrame(
            {
            'organization_name': [],
            'tracking_category_1': [],
            'org_value': [],
            'org_currency': [],
            'group_currency': [],
            'group_value': [],
            'tracking_category_2': [],
            'period': [],
            'actual_or_budget': [],
            'timestamp': []
        })


    for _, hotel in hotels.iterrows():
        categories = get_tracking_categories(hotel['name'], hotel['tenant_id'])
        accounts_df = get_accounts(hotel['name'], hotel['tenant_id'])
        if accounts_df.shape[0]>0:
            cols = [
                "AccountID",
                "Name",
                "ReportingCode",
                "Type",
                "Description",
                "ReportingCodeName",
                "Code"]
            new_accts_cols = {"Code": 'account_code', "Name": "account",
                              "Type": "type", "ReportingCode": 'reporting_code',
                              "ReportingCodeName": "reporting_name","Description":"description"}
            accounts_df = accounts_df[cols].rename(columns=new_accts_cols)
            accounts_df['AccountID'] = accounts_df['AccountID'].astype(str)
            # accounts_df.to_csv(f'{hotel["name"]}_updated_acct.csv', index=False)
        # get tracking options category id **check if all props share the same to hardcode**
        id = list(categories.keys())[0]

        # loop through api call for each tracking
        hotel_trans = []
        for k, v in categories.get(id,{}).items():
            params = {
                'xero_tenant_id': hotel['tenant_id'],
                'from_date': dateutil.parser.parse("2021-12-01"),
                'to_date': dateutil.parser.parse("2021-12-31"),
                'timeframe': 'MONTH',
                'standard_layout': 'True',
                'payments_only': 'false',
                'tracking_category_id':  id,
                # 'tracking_category_id_2': '00000000-0000-0000-0000-000000000000',
                'tracking_option_id': v,
                # 'tracking_option_id_2': '00000000-0000-0000-0000-000000000000'
            }

            # make the call
            try:
                api_response = accounting_api.get_report_profit_and_loss(**params)
            except AccountingBadRequestException as e:
                api_response = ''
                print(
                    "Exception when calling AccountingApi->getReportProfitAndLoss: %s\n"
                    % e)

            # convert response to json
            base = serialize(api_response)['Reports'][0]

            # report = ' - '.join(base['ReportTitles'])
            # pull needed nested info
            for row in base['Rows']:
                if subset := row.get('Rows'):
                    for r in subset:
                        item = r.get('Cells',[{},{}])
                        line = item[0].get('Value')
                        value = item[1].get('Value')
                        acct = item[0].get('Attributes',[{}])[0].get('Value')
                        if acct:
                            hotel_trans.append({
                                'organization_name': hotel['name'],
                                'tracking_category_1': k,
                                'AccountID': acct,
                                'org_value': value
                            })
        hotel_df = pd.DataFrame(hotel_trans)
        if hotel_df.shape[0] > 0 and accounts_df.shape[0] > 0:
            hotel_df['AccountID'] = hotel_df['AccountID'].astype(str)
            hotel_combo_df = accounts_df.merge(hotel_df,
                                               on='AccountID',
                                               how='right').drop(columns='AccountID')
            hotel_combo_df['org_currency'] = hotel['currency']
            hotel_combo_df['group_currency'] = hotel_combo_df['org_currency']
            hotel_combo_df['group_value'] = hotel_combo_df['org_value']
            hotel_combo_df['tracking_category_2'] = 'Unassigned'
            hotel_combo_df['period'] = pd.to_datetime('2021-12-31', utc=True)
            hotel_combo_df['actual_or_budget'] = 'Actual'
            hotel_combo_df['timestamp'] = pd.to_datetime('today', utc=True)
            hotel_combo_df['description'].fillna(' ', inplace=True)

            # hotel_combo_df.to_csv(f'{hotel["name"]}_trans.csv', index=False)
            all_hotels_df = pd.concat([all_hotels_df, hotel_combo_df])
    pd.set_option("display.max_rows", 10_000)
    pd.set_option("display.max_columns", 30)
    all_hotels_df.reset_index(inplace=True, drop=True)
    all_hotels_df.to_csv('all_hotels.csv', index=False)

    return render_template("code.html",
                            title="P&L",
                            # sub_title=report,
                            code=all_hotels_df)


if __name__ == '__main__':
    app.run(host='localhost', port=8000)
