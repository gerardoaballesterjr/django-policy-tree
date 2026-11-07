# Recipe: a retail chain

```
Acme (company)             auditor
└─ North (region)          manager
   ├─ Store 12             cashier
   └─ Store 14             cashier
```

## Models

```python
class Organization(AbstractOrganization):
    type = models.CharField(max_length=20, choices=[
        ("company", "Company"), ("region", "Region"), ("store", "Store")])


class Member(AbstractMember):
    role = models.CharField(max_length=20, choices=[
        ("manager", "Manager"), ("cashier", "Cashier"), ("auditor", "Auditor")])

    class Meta(AbstractMember.Meta):
        pass


class Order(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=10, default="open")


class Refund(models.Model):
    policytree_organization_field = "order__organization"
    policytree_owner_field = "created_by"
    order = models.ForeignKey(Order, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
```

```python
POLICYTREE_ROLE_INCLUDES = {"manager": ["cashier"]}
```

## Conditions

```python
@ConditionRegistry.register
class Paid(Condition):
    name = "paid"
    models = ["sales.Order"]

    def q(self, user):
        return Q(status="paid")


@ConditionRegistry.register
class UnderLimit(Condition):
    name = "under_limit"
    models = ["sales.Order"]
    check_on = Condition.BOTH

    def q(self, user):
        return Q(total__lt=10_000)
```

## Policies

| Role | Model | Action | Effect | Scope | Condition | Fields |
|---|---|---|---|---|---|---|
| manager | order | list, detail, update, delete | allow | tree | | |
| manager | order | delete | deny | tree | paid | |
| manager | refund | create | allow | tree | | |
| cashier | order | create | allow | org | under_limit | write: organization, total |
| cashier | order | list, detail | allow | own | | |
| auditor | order | list, detail | allow | tree | | read: id, total, status |

## API

```python
class OrderViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"refund": ("create", Refund)}

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        order = self.get_object()
        refund = Refund.objects.create(order=order, created_by=request.user, amount=request.data["amount"])
        self.policy_created(refund, submitted={"amount"})   # re-checks where the refund landed
        return Response({"id": refund.pk}, status=201)
```

## Result

| Who | Sees | Can |
|---|---|---|
| North manager | every North order | edit any; delete unpaid only; refund; ring up orders (inherited) |
| Store 12 cashier | orders they rang up | create orders under 10,000 in Store 12, setting only organization and total |
| Company auditor | every order, only id, total, status | read only |
