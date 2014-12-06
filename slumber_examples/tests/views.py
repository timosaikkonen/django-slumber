import logging
from mock import patch
from ujson import dumps, loads

from django.conf import settings
from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase

from slumber import Client
from slumber.connector.ua import _calculate_signature, _fake_http_headers
from slumber.scheme import SlumberServiceURLError
from slumber_examples.models import Order, Pizza, PizzaPrice, Shop
from slumber_examples.tests.configurations import ConfigureUser


def _perform(client, method, url, data, content_type=None, username=None):
    def method_wrapper(*a, **kw):
        logging.info("Using wrapped GET handler")
        return client.get(*a, REQUEST_METHOD=method.upper(), **kw)
    logging.info("%s with data %s", method, data)
    headers = _calculate_signature('service',
        method.upper(), url, data, username)
    response = getattr(client, method, method_wrapper)(
        url, data, content_type=content_type, **_fake_http_headers(headers))
    if response.status_code == 200:
        return response, loads(response.content)
    else:
        return response, {}


class ViewTests(object):
    """Base class for view tests that give us some user agent functionality.
    """
    def do_get(self, url, query = {}, username=None):
        return _perform(self.client, 'get', self.url(url), query,
            username=username)

    def do_post(self, url, body):
        return _perform(self.client, 'post', self.url(url), dumps(body),
            'application/json')

    def do_delete(self, url):
        return _perform(self.client, 'delete', self.url(url), '')

    def do_options(self, url):
        return _perform(self.client, 'options', self.url(url), {})

    def url(self, path):
        if not path.startswith(self.PREFIX + '/'):
            return self.PREFIX + path
        else:
            return path


class PlainTests(object):
    """Used to get non-service based view tests.
    """
    PREFIX = '/slumber'


class ServiceTests(object):
    """Used to get service based view tests.
    """
    PREFIX  = '/slumber/pizzas'
    def patch(self, what, to):
        patcher = patch(what, to)
        self.__patchers.append(patcher)
        patcher.start()
    def setUp(self):
        self.__patchers = []
        self.patch('slumber.server._get_slumber_service', lambda: 'pizzas')
    def tearDown(self):
        [p.stop() for p in self.__patchers]


class ServiceTestsWithDirectory(ServiceTests):
    def setUp(self):
        super(ServiceTestsWithDirectory, self).setUp()
        directory = lambda: dict(pizzas='http://localhost:8000/slumber/pizzas/')
        self.patch('slumber.server._get_slumber_directory', directory)


class ViewErrors(ViewTests):

    def test_method_error(self):
        response, json = self.do_post('/slumber_examples/Pizza/instances/', {})
        self.assertEquals(response.status_code, 405)
        self.assertEquals(response['Allow'], 'GET, OPTIONS')

    def test_invalid_method(self):
        url = self.url('/slumber_examples/Pizza/instances/')
        response = self.client.get(url, REQUEST_METHOD='PURGE',
            HTTP_HOST='localhost', REMOTE_ADDR='127.0.0.1')
        self.assertEquals(response.status_code, 405, response.content)
        self.assertEquals(response['Allow'], 'GET, OPTIONS')

    def test_missing_slash(self):
        response, json = self.do_get('/slumber_examples')
        self.assertEquals(response.status_code, 301)
        self.assertTrue(response['location'].endswith('/slumber_examples/'),
            response['location'])

    def test_invalid_model(self):
        response, json = self.do_get('/slumber_examples/not-a-model/')
        self.assertEquals(response.status_code, 404)

    def test_invalid_model_operation(self):
        response, json = self.do_get('/slumber_examples/Pizza/not-an-operation/')
        self.assertEquals(response.status_code, 404)


class ViewErrorsPlain(ConfigureUser, ViewErrors, PlainTests, TestCase):
    pass
class ViewErrorsService(ConfigureUser, ViewErrors, ServiceTests, TestCase):
    def test_invalid_service(self):
        response = self.client.get('/slumber/not-a-service/')
        self.assertEquals(response.status_code, 404, response.content)


class BasicViews(ViewTests):
    def test_applications(self):
        self.assertTrue(bool(self.user))
        self.assertEqual(self.user.pk, 1)
        response, json = self.do_get('/')
        apps = json['apps']
        self.assertEquals(apps['slumber_examples'], self.url('/slumber_examples/'))
        self.assertTrue(json.has_key('configuration'), json)
        self.assertTrue(json['configuration'].has_key('slumber_examples'), json)
        self.assertTrue(json['configuration']['slumber_examples']['test'], json)

    def test_model_search_success(self):
        response, json = self.do_get('/', {'model': 'slumber_examples.Pizza'})
        self.assertEquals(response.status_code, 302)
        self.assertTrue(response['location'].endswith(
            '/slumber_examples/Pizza/'), response['location'])

    def test_model_search_invalid(self):
        response, json = self.do_get('/', {'model': 'nota.model'})
        self.assertEquals(response.status_code, 404)

    def test_application_with_models(self):
        response, json = self.do_get('/slumber_examples/')
        self.assertEquals(response.status_code, 200)
        self.assertTrue(len(json['models']))
        self.assertEquals(json['models']['Pizza'],
            self.url('/slumber_examples/Pizza/'))

    def test_application_without_models(self):
        response, json = self.do_get('/slumber_examples/no_models/')
        self.assertEquals(response.status_code, 200)
        self.assertFalse(len(json['models']))

    def test_nested_application(self):
        response, json = self.do_get('/slumber_examples/nested1/')
        self.assertEquals(response.status_code, 200, 'slumber_examples.nested1')
    def test_doubly_nested_application(self):
        response, json = self.do_get('/slumber_examples/nested1/nested2/')
        self.assertEquals(response.status_code, 200, 'slumber_examples.nested1.nested2')
    def test_models_across_apps(self):
        response, json = self.do_get('/slumber_ex_shop/NestedModel/')
        self.assertEquals(response.status_code, 200, 'slumber_ex_shop.NestedModel')
        response, json = self.do_get('/slumber_ex_shop/Pizza/')
        self.assertEquals(response.status_code, 404, 'slumber_ex_shop.Pizza')

    def test_instance_metadata_pizza(self):
        response, json = self.do_get('/slumber_examples/Pizza/')
        self.assertEquals(response.status_code, 200)
        self.assertTrue(json['fields'].has_key('for_sale'))
        self.assertEquals(json['fields']['for_sale']['type'],
            'django.db.models.fields.BooleanField')
        self.assertEquals(json['operations']['instances'],
            self.url('/slumber_examples/Pizza/instances/'))
        self.assertFalse(json['operations'].has_key('data'), json['operations'])
        self.assertTrue(json['operations'].has_key('get'), json['operations'])

    def test_instance_metadata_pizzaprice(self):
        response, json = self.do_get('/slumber_examples/PizzaPrice/')
        self.assertEquals(response.status_code, 200)
        self.assertTrue(json['fields'].has_key('pizza'))
        self.assertEquals(json['fields']['pizza']['type'],
            self.url('/slumber_examples/Pizza/'))

    def test_model_metadata_user(self):
        response, json = self.do_get('/django/contrib/auth/User/')
        self.assertEquals(response.status_code, 200)
        self.assertTrue(json['operations'].has_key('authenticate'), json['operations'])
        self.assertEquals(json['operations']['authenticate'],
            self.url('/django/contrib/auth/User/authenticate/'))
        self.assertFalse(json['fields'].has_key('groups'), json['fields'].keys())
        self.assertIn('groups', json['data_arrays'])

    def test_instance_metadata_user(self):
        user = User(username='test-user')
        user.save()
        response, json = self.do_get('/django/contrib/auth/User/data/%s/' %
            user.pk)
        self.assertEquals(response.status_code, 200)
        self.assertTrue(json['operations'].has_key('has-permission'), json['operations'])

    def test_instance_puttable(self):
        response, json = self.do_get('/slumber_examples/Pizza/')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['puttable'], [['id'], ['name']])

    def test_model_operation_instances_no_instances(self):
        response, json = self.do_get('/slumber_examples/Pizza/instances/')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 0)

    def test_model_operation_instances_one_instance(self):
        Pizza(name='S1', for_sale=True).save()
        response, json = self.do_get('/slumber_examples/Pizza/instances/')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 1)

    def test_model_operation_instances_twelve_instances(self):
        for i in range(12):
            Pizza(name='S%s' % i, for_sale=True).save()
        response, json = self.do_get('/slumber_examples/Pizza/instances/')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 10)
        self.assertEquals(json['next_page'],
            self.url('/slumber_examples/Pizza/instances/?start_after=3'))
        response, json = self.do_get('/slumber_examples/Pizza/instances/',
            {'start_after': '3'})
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 2)
        self.assertEquals(json['next_page'],
            self.url('/slumber_examples/Pizza/instances/?start_after=1'))
        response, json = self.do_get('/slumber_examples/Pizza/instances/',
            {'start_after': '1'})
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 0)
        self.assertFalse(json.has_key('next_page'), json)

    def test_instance_creation_get(self):
        response, json = self.do_get('/slumber_examples/Pizza/create/')
        self.assertEquals(response.status_code, 405, response.content)
        self.assertEquals(response['Allow'], 'OPTIONS, POST')

    def test_instance_creation_options(self):
        response, json = self.do_options('/slumber_examples/Pizza/create/')
        self.assertEquals(response.status_code, 200, response.content)
        self.assertEquals(response['Allow'], 'OPTIONS, POST')

    def test_instance_creation_post(self):
        self.user.is_superuser = True
        self.user.save()
        response, json = self.do_post('/slumber_examples/Pizza/create/',
            {'name': 'Test Pizza', 'for_sale': ''})
        self.assertTrue(json.get('identity', '').endswith(
            '/slumber_examples/Pizza/data/1/'), json)
        self.assertEquals(Pizza.objects.count(), 1)
        self.assertEquals(Pizza.objects.all()[0].name, 'Test Pizza')
        self.assertFalse(Pizza.objects.all()[0].for_sale)

    def test_update_instance(self):
        self.user.is_superuser = True
        self.user.save()
        s = Pizza(name='S1', for_sale=True)
        s.save()
        response, json = self.do_post('/slumber_examples/Pizza/update/1/', {
            'name': 'New pizza'})
        self.assertEquals(response.status_code, 200)
        n = Pizza.objects.get(pk=1)
        self.assertEquals(n.name, "New pizza")

    def test_get_instance(self):
        s = Pizza(name='S1', for_sale=True)
        s.save()
        response, json = self.do_get('/slumber_examples/Pizza/')
        get_url = json['operations']['get']
        self.assertEquals(get_url, self.url('/slumber_examples/Pizza/get/'))
        def check_query(query):
            response, json = self.do_get(get_url, query)
            self.assertEquals(response.status_code, 200, response)
            self.assertTrue(json['identity'].endswith(
                '/slumber_examples/Pizza/data/%s/' % s.pk), response)
        check_query({'pk': s.pk})
        check_query({'id': s.pk})
        check_query({'name': s.name})

    def test_instance_data_pizza(self):
        s = Pizza(name='S1', for_sale=True)
        s.save()
        response, json = self.do_get('/slumber_examples/Pizza/data/%s/' % s.pk)
        self.maxDiff = None
        self.assertEquals(json, dict(
            _meta={'message': 'OK', 'status': 200, 'username': 'service'},
            type=self.url('/slumber_examples/Pizza/'),
            identity=self.url('/slumber_examples/Pizza/data/1/'),
            display='S1',
            operations=dict(
                data=self.url('/slumber_examples/Pizza/data/1/'),
                delete=self.url('/slumber_examples/Pizza/delete/1/'),
                order=self.url('/slumber_examples/Pizza/order/1/'),
                update=self.url('/slumber_examples/Pizza/update/1/')),
            fields=dict(
                id=dict(data=s.pk, kind='value', type='django.db.models.fields.AutoField'),
                for_sale=dict(data=s.for_sale, kind='value', type='django.db.models.fields.BooleanField'),
                max_extra_toppings=dict(data=s.max_extra_toppings, kind='value', type='django.db.models.fields.IntegerField'),
                name=dict(data=s.name, kind='value', type='django.db.models.fields.CharField'),
                exclusive_to={'data': None, 'kind': 'object', 'type': self.url('/slumber_examples/Shop/')}),
            data_arrays=dict(
                prices=self.url('/slumber_examples/Pizza/data/%s/prices/' % s.pk))))

    def test_instance_data_shop_with_null_active(self):
        s = Shop(name='Test shop', slug='test-shop')
        s.save()
        response, json = self.do_get('/slumber_examples/Shop/data/%s/' % s.pk)
        self.maxDiff = None
        self.assertEquals(json, dict(
            _meta={'message': 'OK', 'status': 200, 'username': 'service'},
            type=self.url('/slumber_examples/Shop/'),
            identity=self.url('/slumber_examples/Shop/data/1/'),
            display='Test shop',
            operations=dict(
                data='/slumber/pizzas/shop/%s/' % s.pk,
                update=self.url('/slumber_examples/Shop/update/1/')),
            fields=dict(
                id=dict(data=s.pk, kind='value', type='django.db.models.fields.AutoField'),
                active=dict(data=None, kind='value', type='django.db.models.fields.NullBooleanField'),
                name={'data': 'Test shop', 'kind': 'value', 'type': 'django.db.models.fields.CharField'},
                slug={'data': 'test-shop', 'kind': 'value', 'type': 'django.db.models.fields.CharField'},
                web_address={'data': 'http://www.example.com/test-shop/', 'kind': 'property',
                    'type': 'slumber_examples.Shop.web_address'}),
            data_arrays={'pizza': self.url('/slumber_examples/Shop/data/1/pizza/')}))

    def test_instance_data_pizzaprice(self):
        s = Pizza(name='p1', for_sale=True)
        s.save()
        p = PizzaPrice(pizza=s, date='2010-01-01')
        p.save()
        response, json = self.do_get('/slumber_examples/PizzaPrice/data/%s/' % p.pk)
        self.assertEquals(json, dict(
            _meta={'message': 'OK', 'status': 200, 'username': 'service'},
            type=self.url('/slumber_examples/PizzaPrice/'),
            identity=self.url('/slumber_examples/PizzaPrice/data/1/'),
            display="PizzaPrice object",
            operations=dict(
                data=self.url('/slumber_examples/PizzaPrice/data/1/'),
                delete=self.url('/slumber_examples/PizzaPrice/delete/1/'),
                update=self.url('/slumber_examples/PizzaPrice/update/1/')),
            fields=dict(
                id={'data': 1, 'kind': 'value', 'type': 'django.db.models.fields.AutoField'},
                pizza={'data': {
                        'type': self.url('/slumber_examples/Pizza/'), 'display':'p1',
                        'data': self.url('/slumber_examples/Pizza/data/1/')},
                    'kind': 'object', 'type': self.url('/slumber_examples/Pizza/')},
                date={'data': '2010-01-01', 'kind': 'value', 'type': 'django.db.models.fields.DateField'},
            ),
            data_arrays={'amounts': self.url('/slumber_examples/PizzaPrice/data/1/amounts/')}))

    def test_instance_data_array(self):
        s = Pizza(name='P', for_sale=True)
        s.save()
        for p in range(15):
            PizzaPrice(pizza=s, date='2011-04-%s' % (p+1)).save()
        response, json = self.do_get('/slumber_examples/Pizza/data/%s/prices/' % s.pk)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 10, json)
        self.assertTrue(json.has_key('next_page'), json)
        self.assertEquals(json['next_page'],
            self.url('/slumber_examples/Pizza/data/1/prices/?start_after=6'),
            json['next_page'])
        response, json = self.do_get('/slumber_examples/Pizza/data/1/prices/',
            {'start_after': '6'})
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(json['page']), 5)
        self.assertEquals(json['page'][0], {
            'type': self.url('/slumber_examples/PizzaPrice/'),
            'pk': 5, 'data': self.url('/slumber_examples/PizzaPrice/data/5/'), 'display': 'PizzaPrice object'})
        self.assertFalse(json.has_key('next_page'), json.keys())

    def test_delete_instance_with_post(self):
        self.user.is_superuser = True
        self.user.save()
        s = Pizza(name='P')
        s.save()
        response, json = self.do_get('/slumber_examples/Pizza/data/%s/' % s.pk)
        self.assertEquals(response.status_code, 200)
        self.assertTrue(json['operations'].has_key('delete'), json['operations'])
        response, json = self.do_post(json['operations']['delete'], {})
        self.assertEquals(response.status_code, 200)
        with self.assertRaises(Pizza.DoesNotExist):
            Pizza.objects.get(pk=s.pk)

    def test_delete_instance_with_delete(self):
        self.user.is_superuser = True
        self.user.save()
        s = Pizza(name='P')
        s.save()
        response, json = self.do_get('/slumber_examples/Pizza/data/%s/' % s.pk)
        self.assertEquals(response.status_code, 200)
        response, _json = self.do_delete(json['operations']['data'])
        self.assertEquals(response.status_code, 200)
        with self.assertRaises(Pizza.DoesNotExist):
            Pizza.objects.get(pk=s.pk)
        response, _json = self.do_delete(json['operations']['data'])
        self.assertEquals(response.status_code, 404)



class BasicViewsPlain(ConfigureUser, BasicViews, PlainTests, TestCase):
    def test_service_configuration_missing_for_remoteforeignkey(self):
        self.user.is_superuser = True
        self.user.save()
        client = Client()
        shop = client.slumber_examples.Shop.create(name="Home", slug='home')
        order = Order(shop=shop)
        order.save()
        self.assertIsNotNone(order.shop)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT shop FROM slumber_examples_order WHERE id=%s",
            [order.pk])
        row = cursor.fetchone()
        self.assertEquals(row[0], order.shop._url)
        with self.assertRaises(SlumberServiceURLError):
            order2 = Order.objects.get(pk=order.pk)


class BasicViewsService(ConfigureUser, BasicViews, ServiceTests, TestCase):
    def test_services_with_directory(self):
        with patch('slumber.server.get_slumber_directory', lambda: {
                'pizzas': 'http://localhost:8000:/slumber/pizzas/',
                'takeaway': 'http://localhost:8002:/slumber/'}):
            response = self.client.get('/slumber/',
                HTTP_HOST='localhost', REMOTE_ADDR='127.0.0.1')
        json = loads(response.content)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json['services'].get('pizzas', None),
            'http://localhost:8000:/slumber/pizzas/', json)
        self.assertEqual(json['services'].get('takeaway', None),
            'http://localhost:8002:/slumber/', json)

    def test_services_without_directory(self):
        response = self.client.get('/slumber/',
            HTTP_HOST='localhost', REMOTE_ADDR='127.0.0.1')
        json = loads(response.content)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json['services'].get('pizzas', '').endswith('/slumber/pizzas/'), json)

    def test_service_configuration_works_for_remoteforeignkey(self):
        self.user.is_superuser = True
        self.user.save()
        client = Client()
        shop = client.pizzas.slumber_examples.Shop.create(name="Home", slug='home')
        order = Order(shop=shop)
        order.save()
        self.assertIsNotNone(order.shop)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT shop FROM slumber_examples_order WHERE id=%s",
            [order.pk])
        row = cursor.fetchone()
        self.assertEquals(row[0], 'slumber://pizzas/shop/1/')
        order2 = Order.objects.get(pk=order.pk)
        self.assertEquals(unicode(order2.shop), unicode(order.shop))
        self.assertEquals(order2.shop.id, order.shop.id)


class BasicViewsWithServiceDirectory(ConfigureUser, BasicViews,
        ServiceTestsWithDirectory, TestCase):
    def test_service_configuration_works_for_remoteforeignkey(self):
        self.user.is_superuser = True
        self.user.save()
        client = Client()
        shop = client.pizzas.slumber_examples.Shop.create(name="Home", slug='home')
        order = Order(shop=shop)
        order.save()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT shop FROM slumber_examples_order WHERE id=%s",
            [order.pk])
        row = cursor.fetchone()
        self.assertEquals(row[0],
            'slumber://pizzas/shop/%s/' % shop.id)
        order2 = Order.objects.get(pk=order.pk)
        self.assertEquals(unicode(order2.shop), unicode(order.shop))
        self.assertEquals(order2.shop.id, order.shop.id)


class UserViews(ViewTests):
    authn = '/django/contrib/auth/User/authenticate/'
    data = '/django/contrib/auth/User/data/%s/'
    perm = '/django/contrib/auth/User/has-permission/%s/%s/'
    perms = '/django/contrib/auth/User/get-permissions/%s/'
    user_perm = '/django/contrib/auth/User/do-i-have-perm/%s/'
    user_perm_q = '/django/contrib/auth/User/do-i-have-perm/'

    def setUp(self):
        self.user = User(username='test-user')
        self.user.set_password('password')
        self.user.save()
        # The actual model doesn't matter so long as it is in auth
        self.content_type = ContentType.objects.get(model='user')
        super(UserViews, self).setUp()

    def test_user_data(self):
        response, json = self.do_get(self.data % self.user.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn('is_superuser', json['fields'].keys())
        self.assertIn('date_joined', json['fields'].keys())

    def test_user_not_found(self):
        response, json = self.do_post(self.authn, dict(username='not-a-user', password=''))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['authenticated'], False, json)
        self.assertIsNone(json['user'], json)

    def test_user_wrong_password(self):
        response, json = self.do_post(self.authn,
            dict(username=self.user.username, password='wrong'))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['authenticated'], False, json)
        self.assertIsNone(json['user'], json)

    def test_user_authenticates(self):
        response, json = self.do_post(self.authn,
            dict(username=self.user.username, password='password'))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['authenticated'], True, json)
        self.assertDictContainsSubset(
            {'pk': self.user.pk, 'display_name': 'test-user'},
            json['user'])
        self.assertTrue(
            json['user']['url'].endswith('/django/contrib/auth/User/data/3/'),
            json['user']['url'])

    def test_user_permission_no_permission(self):
        response, json = self.do_get(self.perm % (self.user.pk, 'foo.example'))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['is-allowed'], False, json)

    def test_current_user_permission_no_permission(self):
        response, json = self.do_get(self.user_perm % 'foo.example',
            username=self.user.username)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['permissions']['foo.example'], False, json)

    def test_user_permission_is_allowed(self):
        permission = Permission(content_type=self.content_type,
            name='Can something', codename='can_something')
        permission.save()
        self.user.user_permissions.add(permission)
        response, json = self.do_get(self.perm % (self.user.pk, 'auth.can_something'))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['is-allowed'], True, json)

    def test_current_user_permission_is_allowed(self):
        permission = Permission(content_type=self.content_type,
            name='Can something', codename='can_something')
        permission.save()
        self.user.user_permissions.add(permission)
        response, json = self.do_get(self.user_perm % 'auth.can_something',
            username=self.user.username)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['permissions']['auth.can_something'], True, json)

    def test_user_permission_not_allowed(self):
        permission = Permission(content_type=self.content_type,
            name='Can something', codename='can_something')
        permission.save()
        response, json = self.do_get(self.perm % (self.user.pk, 'auth.can_something'))
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['is-allowed'], False, json)

    def test_current_user_permission_not_allowed(self):
        permission = Permission(content_type=self.content_type,
            name='Can something', codename='can_something')
        permission.save()
        response, json = self.do_get(self.user_perm % 'auth.can_something',
                username=self.user.username)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['permissions']['auth.can_something'], False, json)

    def test_current_user_multiple_permissions(self):
        can_perm = Permission(content_type=self.content_type,
            name='Can something', codename='can_something')
        can_perm.save()
        self.user.user_permissions.add(can_perm)
        cannot_perm = Permission(content_type=self.content_type,
            name='Cannot something', codename='cannot_something')
        cannot_perm.save()
        response, json = self.do_get(self.user_perm_q,
            dict(q=['foo.example', 'auth.can_something', 'auth.cannot_something']),
            username=self.user.username)
        self.assertEquals(response.status_code, 200)
        permissions = json['permissions']
        self.assertEquals(permissions['foo.example'], False, json)
        self.assertEquals(permissions['auth.can_something'], True, json)
        self.assertEquals(permissions['auth.cannot_something'], False, json)

    def test_current_user_no_permissions(self):
        response, json = self.do_get(self.user_perm_q,
            username=self.user.username)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(json['permissions'].keys(), [], json)

    def test_get_group_permissions(self):
        response, json = self.do_get(self.perms % self.user.pk)
        self.assertEquals(response.status_code, 200)
        self.assertItemsEqual(json['group_permissions'], [])


class UserViewsPlain(ConfigureUser, UserViews, PlainTests, TestCase):
    pass
class UserViewsService(ConfigureUser, UserViews, ServiceTests, TestCase):
    pass

