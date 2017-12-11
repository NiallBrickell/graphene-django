from functools import partial

from django.db.models.query import QuerySet

from promise import Promise

from graphene.types import Field, List
from graphene.relay import ConnectionField, PageInfo
from graphql_relay.connection.arrayconnection import connection_from_list_slice

from .settings import graphene_settings
from .utils import maybe_queryset


class DjangoListField(Field):

    def __init__(self, _type, *args, **kwargs):
        super(DjangoListField, self).__init__(List(_type), *args, **kwargs)

    @property
    def model(self):
        return self.type.of_type._meta.node._meta.model

    @staticmethod
    def list_resolver(resolver, root, info, **args):
        return maybe_queryset(resolver(root, info, **args))

    def get_resolver(self, parent_resolver):
        return partial(self.list_resolver, parent_resolver)


class DjangoConnectionField(ConnectionField):

    def __init__(self, *args, **kwargs):
        self.on = kwargs.pop('on', False)
        self.max_limit = kwargs.pop(
            'max_limit',
            graphene_settings.RELAY_CONNECTION_MAX_LIMIT
        )
        self.enforce_first_or_last = kwargs.pop(
            'enforce_first_or_last',
            graphene_settings.RELAY_CONNECTION_ENFORCE_FIRST_OR_LAST
        )
        super(DjangoConnectionField, self).__init__(*args, **kwargs)

    @property
    def type(self):
        from .types import DjangoObjectType
        _type = super(ConnectionField, self).type
        assert issubclass(_type, DjangoObjectType), "DjangoConnectionField only accepts DjangoObjectType types"
        assert _type._meta.connection, "The type {} doesn't have a connection".format(_type.__name__)
        return _type._meta.connection

    @property
    def node_type(self):
        return self.type._meta.node

    @property
    def model(self):
        return self.node_type._meta.model

    def get_manager(self):
        if self.on:
            return getattr(self.model, self.on)
        else:
            return self.model._default_manager

    @staticmethod
    def merge_querysets(default_queryset, queryset):
        # There could be the case where the default queryset (returned from the filterclass)
        # and the resolver queryset have some limits on it.
        # We only would be able to apply one of those, but not both
        # at the same time.

        # See related PR: https://github.com/graphql-python/graphene-django/pull/126

        assert not (default_queryset.query.low_mark and queryset.query.low_mark), (
            'Received two sliced querysets (low mark) in the connection, please slice only in one.'
        )
        assert not (default_queryset.query.high_mark and queryset.query.high_mark), (
            'Received two sliced querysets (high mark) in the connection, please slice only in one.'
        )
        low = default_queryset.query.low_mark or queryset.query.low_mark
        high = default_queryset.query.high_mark or queryset.query.high_mark
        default_queryset.query.clear_limits()
        queryset = default_queryset & queryset
        queryset.query.set_limits(low, high)
        return queryset

    @classmethod
    def resolve_connection(cls, connection, default_manager, args, iterable):
        if iterable is None:
            iterable = default_manager
        iterable = maybe_queryset(iterable)
        if isinstance(iterable, QuerySet):
            if iterable is not default_manager:
                default_queryset = maybe_queryset(default_manager)
                iterable = cls.merge_querysets(default_queryset, iterable)
            _len = iterable.count()
        else:
            _len = len(iterable)
        connection = connection_from_list_slice(
            iterable,
            args,
            slice_start=0,
            list_length=_len,
            list_slice_length=_len,
            connection_type=connection,
            edge_type=connection.Edge,
            pageinfo_type=PageInfo,
        )
        connection.iterable = iterable
        connection.length = _len
        return connection

    @classmethod
    def connection_resolver(cls, resolver, connection, default_manager, max_limit,
                            enforce_first_or_last, root, info, **args):
        first = args.get('first')
        last = args.get('last')

        if enforce_first_or_last:
            assert first or last, (
                'You must provide a `first` or `last` value to properly paginate the `{}` connection.'
            ).format(info.field_name)

        if max_limit:
            if first:
                assert first <= max_limit, (
                    'Requesting {} records on the `{}` connection exceeds the `first` limit of {} records.'
                ).format(first, info.field_name, max_limit)
                args['first'] = min(first, max_limit)

            if last:
                assert last <= max_limit, (
                    'Requesting {} records on the `{}` connection exceeds the `last` limit of {} records.'
                ).format(first, info.field_name, max_limit)
                args['last'] = min(last, max_limit)

        iterable = resolver(root, info, **args)
        on_resolve = partial(cls.resolve_connection, connection, default_manager, args)

        if Promise.is_thenable(iterable):
            return Promise.resolve(iterable).then(on_resolve)

        return on_resolve(iterable)

    def get_resolver(self, parent_resolver):
        return partial(
            self.connection_resolver,
            parent_resolver,
            self.type,
            self.get_manager(),
            self.max_limit,
            self.enforce_first_or_last
        )
