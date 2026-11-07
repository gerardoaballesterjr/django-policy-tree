from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from policytree.drf import PolicyFieldsMixin, PolicyPermission, PolicyQuerysetMixin

from .models import Enrollment, Invoice, Organization, Room, Space


class OrganizationSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "code", "type", "parent", "path"]
        read_only_fields = ["path"]


class EnrollmentSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Enrollment
        fields = ["id", "organization", "owner", "student_name", "grade", "tuition"]
        read_only_fields = ["owner"]


class InvoiceSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ["id", "number", "amount", "notes", "organization", "enrollment", "owner"]
        read_only_fields = ["organization", "enrollment", "owner"]


class EnrollmentWithInvoicesSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    invoices = InvoiceSerializer(many=True, read_only=True)

    class Meta:
        model = Enrollment
        fields = ["id", "student_name", "invoices"]


class OrganizationViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Organization.objects.all().order_by("path")
    serializer_class = OrganizationSerializer
    permission_classes = [PolicyPermission]


class EnrollmentViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.select_related("organization").order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {
        "approve": "update",                          # own model
        "bill": ("create", Invoice),                  # second model, on an object
        "invoices": ("list", "testapp.Invoice"),      # second model by label
        "issue": ("create", Invoice),                 # second model, no object
        "grade_and_bill": [("update", None), ("create", Invoice)],  # both
        "full": "detail",
    }

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        enrollment = self.get_object()  # needs "update" on it (policy_actions)...
        self.policy_skip(Enrollment, "update")  # ...but changes nothing: a permission gate only
        return Response({"approved": enrollment.pk})

    @action(detail=True, methods=["post"])
    def bill(self, request, pk=None):
        enrollment = self.get_object()
        ser = InvoiceSerializer(data=request.data, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        ser.save(enrollment=enrollment, organization=enrollment.organization, owner=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    @action(detail=True)
    def invoices(self, request, pk=None):
        enrollment = self.get_object()
        qs = self.policy_queryset(Invoice.objects.filter(enrollment=enrollment).order_by("id"))
        ser = InvoiceSerializer(qs, many=True, context=self.get_serializer_context())
        return Response(ser.data)

    @action(detail=False, methods=["post"])
    def issue(self, request):
        org = Organization.objects.get(pk=request.data["organization"])
        ser = InvoiceSerializer(data=request.data, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        ser.save(organization=org, owner=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def grade_and_bill(self, request, pk=None):
        enrollment = self.get_object()
        before = self.policy.place_of(enrollment)
        enrollment.grade = request.data.get("grade", enrollment.grade)
        enrollment.save()
        self.policy_changed(enrollment, before)  # changed without a serializer: re-check it
        invoice = Invoice.objects.create(
            enrollment=enrollment, organization=enrollment.organization,
            owner=request.user, number="G-1", amount=request.data.get("amount", 0),
        )
        self.policy_created(invoice, submitted={"amount"})  # created without a serializer: re-check it
        return Response({"ok": True})

    @action(detail=True)
    def full(self, request, pk=None):
        enrollment = self.get_object()
        ser = EnrollmentWithInvoicesSerializer(enrollment, context=self.get_serializer_context())
        return Response(ser.data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        return Response({"rows": self.get_queryset().count()})


class SpaceSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Space
        fields = ["id", "name", "code", "branch"]


class RoomSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Room
        fields = ["id", "name", "code", "capacity", "space"]


class SpaceWithRoomsSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    rooms = RoomSerializer(many=True, read_only=True)
    room_list = RoomSerializer(many=True, read_only=True)

    class Meta:
        model = Space
        fields = ["id", "name", "code", "rooms", "room_list"]


class SpaceViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Space.objects.select_related("branch").order_by("id")
    serializer_class = SpaceSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"scoped": "list", "plain": "list"}

    def _nested(self, queryset):
        ser = SpaceWithRoomsSerializer(queryset, many=True, context=self.get_serializer_context())
        return Response(ser.data)

    @action(detail=False)
    def scoped(self, request):
        """Nested rooms through policy_prefetch."""
        return self._nested(self.get_queryset().prefetch_related(self.policy_prefetch("rooms", Room.objects.all())))

    @action(detail=False)
    def plain(self, request):
        """Nested rooms through an ordinary prefetch: still filtered, in memory."""
        return self._nested(self.get_queryset().prefetch_related("rooms"))


class RoomViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Room.objects.select_related("space__branch").order_by("id")
    serializer_class = RoomSerializer
    permission_classes = [PolicyPermission]
