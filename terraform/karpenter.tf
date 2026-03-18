# ── Karpenter IAM + SQS — via EKS module's Karpenter submodule ───────────
# Sets up:
#   - Node IAM role (assigned to EC2 instances Karpenter provisions)
#   - Controller IAM role (IRSA — allows Karpenter pod to call EC2 APIs)
#   - SQS queue for spot interruption / health events (graceful draining)
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.0"

  cluster_name = module.eks.cluster_name

  # Use Karpenter v1 permissions model (required for chart >= 1.0.0)
  enable_v1_permissions = true

  # EKS Pod Identity association (preferred over IRSA for Karpenter >= 0.37)
  enable_pod_identity             = true
  create_pod_identity_association = true

  # Allow SSM access on Karpenter-provisioned nodes for debugging
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }
}

# ── Karpenter Helm Release ────────────────────────────────────────────────
# Installs the Karpenter controller into the karpenter namespace.
# IMPORTANT: Run `terraform apply -target=module.eks` first so the EKS
# cluster and OIDC provider exist before Helm can authenticate.
resource "helm_release" "karpenter" {
  namespace        = "karpenter"
  create_namespace = true

  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  wait       = true
  timeout    = 300

  values = [
    jsonencode({
      settings = {
        clusterName       = module.eks.cluster_name
        clusterEndpoint   = module.eks.cluster_endpoint
        interruptionQueue = module.karpenter.queue_name
      }
      serviceAccount = {
        annotations = {
          "eks.amazonaws.com/role-arn" = module.karpenter.iam_role_arn
        }
      }
      # Keep controller footprint minimal on the managed node group
      resources = {
        requests = { cpu = "100m", memory = "256Mi" }
        limits   = { cpu = "500m", memory = "512Mi" }
      }
    })
  ]

  depends_on = [module.eks.eks_managed_node_groups]
}

# ── EC2NodeClass — shared node configuration for both NodePools ───────────
# Defines: AMI family, IAM role, subnet/SG discovery tags
resource "kubectl_manifest" "karpenter_node_class" {
  yaml_body = <<-YAML
    apiVersion: karpenter.k8s.aws/v1
    kind: EC2NodeClass
    metadata:
      name: default
    spec:
      # Amazon Linux 2023 — lightweight, fast boot, EKS-optimised
      amiSelectorTerms:
        - alias: al2023@latest
      # IAM role that Karpenter-provisioned nodes assume
      role: "${module.karpenter.node_iam_role_name}"
      # Discover subnets and security groups via the cluster tag
      subnetSelectorTerms:
        - tags:
            karpenter.sh/discovery: "${var.cluster_name}"
      securityGroupSelectorTerms:
        - tags:
            karpenter.sh/discovery: "${var.cluster_name}"
      # Minimal root volume
      blockDeviceMappings:
        - deviceName: /dev/xvda
          ebs:
            volumeSize: 30Gi
            volumeType: gp3
            deleteOnTermination: true
      tags:
        karpenter.sh/discovery: "${var.cluster_name}"
        ManagedBy: karpenter
  YAML

  depends_on = [helm_release.karpenter]
}

# ── NodePool: light-agents ────────────────────────────────────────────────
# Used by: Scriptwriter, Avatar Director, SEO Publisher
# Strategy: Spot instances, scale-to-zero after 60s idle
# Burst capacity handles parallel pipeline runs
resource "kubectl_manifest" "karpenter_nodepool_light" {
  yaml_body = <<-YAML
    apiVersion: karpenter.sh/v1
    kind: NodePool
    metadata:
      name: light-agents
    spec:
      template:
        metadata:
          labels:
            node.kubernetes.io/pool: light-agents
        spec:
          nodeClassRef:
            group: karpenter.k8s.aws
            kind: EC2NodeClass
            name: default
          requirements:
            - key: kubernetes.io/arch
              operator: In
              values: ["amd64", "arm64"]
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["spot", "on-demand"]
            - key: node.kubernetes.io/instance-type
              operator: In
              # t3/t3a variants provide adequate RAM for API-bound workloads
              values: ["t3.medium", "t3.large", "t3a.medium", "t3a.large"]
      limits:
        cpu: 20
        memory: 40Gi
      disruption:
        consolidationPolicy: WhenEmpty
        consolidateAfter: 60s
  YAML

  depends_on = [kubectl_manifest.karpenter_node_class]
}

# ── NodePool: video-editor ────────────────────────────────────────────────
# Used by: Video Editor (FFmpeg rendering) ONLY
# Strategy: On-demand compute-optimised, tainted to prevent accidental use,
#           aggressively reclaimed after 30s idle (key FinOps control)
resource "kubectl_manifest" "karpenter_nodepool_video_editor" {
  yaml_body = <<-YAML
    apiVersion: karpenter.sh/v1
    kind: NodePool
    metadata:
      name: video-editor
    spec:
      template:
        metadata:
          labels:
            node.kubernetes.io/pool: video-editor
        spec:
          nodeClassRef:
            group: karpenter.k8s.aws
            kind: EC2NodeClass
            name: default
          requirements:
            - key: kubernetes.io/arch
              operator: In
              values: ["amd64"]
            # On-demand only — rendering must not be interrupted mid-encode
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["on-demand"]
            # c5.2xlarge: 8 vCPU, 16 GiB — enough for 1080p FFmpeg pipeline
            # c5.4xlarge as fallback if 2xlarge unavailable in the AZ
            - key: node.kubernetes.io/instance-type
              operator: In
              values: ["c5.2xlarge", "c5.4xlarge", "c5a.2xlarge", "c5a.4xlarge"]
          # Taint prevents ANY pod except the Video Editor job from landing here
          taints:
            - key: workload
              value: video-editor
              effect: NoSchedule
      limits:
        cpu: 16       # max 2 concurrent renders (unlikely but safe upper bound)
        memory: 64Gi
      disruption:
        consolidationPolicy: WhenEmpty
        # 30s — node is gone within ~1 minute of the FFmpeg job finishing
        consolidateAfter: 30s
  YAML

  depends_on = [kubectl_manifest.karpenter_node_class]
}
