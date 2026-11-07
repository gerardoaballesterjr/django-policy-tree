from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from policytree.views import PolicyViewMixin

from .models import Enrollment


class EnrollmentPage:
    model = Enrollment
    template_name = "testapp/page.html"
    success_url = reverse_lazy("web-list")


class EnrollmentListView(PolicyViewMixin, EnrollmentPage, ListView):
    pass


class EnrollmentDetailView(PolicyViewMixin, EnrollmentPage, DetailView):
    pass


class EnrollmentCreateView(PolicyViewMixin, EnrollmentPage, CreateView):
    fields = ["organization", "student_name", "grade", "tuition"]

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)


class EnrollmentUpdateView(PolicyViewMixin, EnrollmentPage, UpdateView):
    fields = ["organization", "student_name", "grade", "tuition"]


class EnrollmentDeleteView(PolicyViewMixin, EnrollmentPage, DeleteView):
    pass
