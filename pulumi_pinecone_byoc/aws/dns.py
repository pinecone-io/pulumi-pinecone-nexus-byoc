import pulumi
import pulumi_aws as aws

from ..common.naming import DNS_CNAMES
from ..common.providers import DnsDelegation, DnsDelegationArgs


class DNS(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        subdomain: pulumi.Input[str],
        parent_zone_name: pulumi.Input[str],
        api_url: pulumi.Input[str],
        cpgw_api_key: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("pinecone:byoc:DNS", name, None, opts)

        child_opts = pulumi.ResourceOptions(parent=self)

        tags = {"pinecone:managed-by": "pulumi"}

        def build_fqdn(sub: str) -> str:
            return f"{sub}.{parent_zone_name}"

        fqdn = pulumi.Output.from_input(subdomain).apply(build_fqdn)

        self.zone = aws.route53.Zone(
            f"{name}-zone",
            name=fqdn,
            force_destroy=True,
            tags={**tags, "Name": f"{name}-zone"},
            opts=child_opts,
        )

        self.delegation = DnsDelegation(
            f"{name}-delegation",
            DnsDelegationArgs(
                subdomain=subdomain,
                nameservers=self.zone.name_servers,
                api_url=api_url,
                cpgw_api_key=cpgw_api_key,
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.zone]),
        )

        # create CNAME records pointing to ingress (public ALB)
        # these enable public access to data plane via the internet-facing ALB
        for cname in DNS_CNAMES:
            # use default arg to capture cname value at loop time (avoid closure issue)
            aws.route53.Record(
                f"{name}-{cname.replace('*', 'wildcard').replace('.', '-')}-cname",
                zone_id=self.zone.id,
                name=fqdn.apply(lambda f, c=cname: f"{c}.{f}"),
                type="CNAME",
                records=[fqdn.apply(lambda f: f"ingress.{f}")],
                ttl=300,
                allow_overwrite=True,
                opts=child_opts,
            )

        # create ACM certificate - include *.svc and *.wksp subdomains for data
        # plane and workspace endpoints (wildcard certs only match one level, so
        # each nested level needs its own SAN)
        self.certificate = aws.acm.Certificate(
            f"{name}-cert",
            domain_name=fqdn.apply(lambda f: f"*.{f}"),
            subject_alternative_names=[
                fqdn,
                fqdn.apply(lambda f: f"*.svc.{f}"),
                fqdn.apply(lambda f: f"*.wksp.{f}"),
            ],
            validation_method="DNS",
            tags={**tags, "Name": f"{name}-cert"},
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self.delegation],
                retain_on_delete=True,  # cert may be in use by ALBs
            ),
        )

        # DNS validation records, one per unique record ACM asks for. ACM reuses
        # the same validation record across domains sharing a base (`*.{fqdn}`
        # and `{fqdn}` here), so iterate domain_validation_options and dedupe by
        # record name rather than assuming one record per cert domain.
        validation_records = self.certificate.domain_validation_options.apply(
            lambda opts: self._create_validation_records(
                f"{name}-cert-validation", opts, child_opts
            )
        )
        validation_record_fqdns = validation_records.apply(
            lambda records: pulumi.Output.all(*[r.fqdn for r in records])
        )

        # Certificate validation
        self.certificate_validation = aws.acm.CertificateValidation(
            f"{name}-cert-validation",
            certificate_arn=self.certificate.arn,
            # Derived from the records' outputs, so this also sequences the
            # validation after the records exist.
            validation_record_fqdns=validation_record_fqdns,
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.certificate]),
        )

        # private endpoint certificate - for PrivateLink access
        # these domains use .private suffix pattern
        private_cnames = [f"{c}.private" for c in DNS_CNAMES]
        self._private_dns_domains = [fqdn.apply(lambda f, c=c: f"{c}.{f}") for c in private_cnames]

        self.private_certificate = aws.acm.Certificate(
            f"{name}-private-cert",
            domain_name=self._private_dns_domains[0],
            subject_alternative_names=self._private_dns_domains[1:],
            validation_method="DNS",
            tags={**tags, "Name": f"{name}-private-cert"},
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self.delegation],
                retain_on_delete=True,
            ),
        )

        # number of unique validation records depends on domain count
        private_validation_records = []
        for i in range(len(private_cnames)):
            private_validation_record = aws.route53.Record(
                f"{name}-private-cert-validation-{i}",
                zone_id=self.zone.id,
                name=self.private_certificate.domain_validation_options[i].resource_record_name,
                type=self.private_certificate.domain_validation_options[i].resource_record_type,
                records=[
                    self.private_certificate.domain_validation_options[i].resource_record_value
                ],
                ttl=300,
                allow_overwrite=True,
                opts=child_opts,
            )
            private_validation_records.append(private_validation_record)

        self.private_certificate_validation = aws.acm.CertificateValidation(
            f"{name}-private-cert-validation",
            certificate_arn=self.private_certificate.arn,
            validation_record_fqdns=[r.fqdn for r in private_validation_records],
            opts=pulumi.ResourceOptions(parent=self, depends_on=private_validation_records),
        )

        self._fqdn = fqdn
        self._subdomain = subdomain

        self.register_outputs(
            {
                "zone_id": self.zone.id,
                "zone_name_servers": self.zone.name_servers,
                "certificate_arn": self.certificate_validation.certificate_arn,
                "private_certificate_arn": self.private_certificate_validation.certificate_arn,
                "fqdn": fqdn,
            }
        )

    def _create_validation_records(
        self,
        prefix: str,
        options: list,
        opts: pulumi.ResourceOptions,
    ) -> list[aws.route53.Record]:
        """One Route53 record per unique ACM validation record.

        Runs inside an ``apply`` on ``domain_validation_options`` (the option
        list is only known once the certificate exists), deduping by resource
        record name so two cert domains that ACM validates with the same record
        don't produce two Pulumi resources owning one RR set.
        """
        records: list[aws.route53.Record] = []
        seen: set[str] = set()
        for option in options or []:
            if option.resource_record_name in seen:
                continue
            seen.add(option.resource_record_name)
            records.append(
                aws.route53.Record(
                    f"{prefix}-{len(records)}",
                    zone_id=self.zone.id,
                    name=option.resource_record_name,
                    type=option.resource_record_type,
                    records=[option.resource_record_value],
                    ttl=300,
                    allow_overwrite=True,
                    opts=opts,
                )
            )
        return records

    @property
    def zone_id(self) -> pulumi.Output[str]:
        return self.zone.id

    @property
    def fqdn(self) -> pulumi.Output[str]:
        return self._fqdn

    @property
    def name_servers(self) -> pulumi.Output:
        return self.zone.name_servers

    @property
    def certificate_arn(self) -> pulumi.Output[str]:
        return self.certificate_validation.certificate_arn

    @property
    def subdomain(self) -> pulumi.Input[str]:
        return self._subdomain

    @property
    def private_certificate_arn(self) -> pulumi.Output[str]:
        return self.private_certificate_validation.certificate_arn

    @property
    def private_dns_domains(self) -> list[pulumi.Output[str]]:
        return self._private_dns_domains
