# ── Reuse existing VPC — look up by ID from tfvars ───────────────────────
data "aws_vpc" "main" {
  id = var.vpc_id
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Look up the existing internet gateway attached to the VPC
data "aws_internet_gateway" "main" {
  filter {
    name   = "attachment.vpc-id"
    values = [data.aws_vpc.main.id]
  }
}

# ── Private Subnets — EKS nodes + Karpenter-provisioned pods ─────────────
# Two subnets across two AZs: required by EKS for high-availability
# CIDR defaults (172.31.64/80) sit above the default-VPC /20 blocks
# (0, 16, 32, 48) and should not conflict; override via tfvars if needed.
resource "aws_subnet" "private" {
  count = 2

  vpc_id            = data.aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index]

  # EKS tags — required for the AWS cloud-provider and Karpenter discovery
  tags = {
    Name                                          = "yt-factory-private-${count.index + 1}"
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/${var.cluster_name}"   = "owned"
    "karpenter.sh/discovery"                      = var.cluster_name
  }
}

# ── Public Subnets — NAT Gateway egress + future ALB ─────────────────────
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = data.aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false # instances never need direct public IPs

  tags = {
    Name                                          = "yt-factory-public-${count.index + 1}"
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/cluster/${var.cluster_name}"   = "owned"
  }
}

# ── NAT Gateway — single NAT (cost-optimised; add second for HA if needed)
# Sits in public[0] so private-subnet pods can reach HeyGen, OpenAI, YouTube
resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [data.aws_internet_gateway.main]
  tags       = { Name = "yt-factory-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "yt-factory-nat" }
  depends_on    = [data.aws_internet_gateway.main]
}

# ── Route Tables ─────────────────────────────────────────────────────────
resource "aws_route_table" "private" {
  vpc_id = data.aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "yt-factory-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table" "public" {
  vpc_id = data.aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = data.aws_internet_gateway.main.id
  }

  tags = { Name = "yt-factory-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ── S3 Gateway VPC Endpoint ───────────────────────────────────────────────
# Routes S3 traffic through AWS backbone — eliminates NAT data-processing
# charges for all agent S3 reads/writes (major FinOps saving at scale)
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = data.aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "yt-factory-s3-vpce" }
}
