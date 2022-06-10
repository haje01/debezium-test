variable region {
  default = "ap-northeast-2"
}
variable "name" {}
variable "ubuntu_ami" {}
variable "mssql_ami" {}
variable "mssql_instance_type" {
  default = "m5.xlarge"
}
variable "insel_instance_type" {
  default = "t3.medium"
}
variable "kafka_instance_type" {
  default = "t3.medium"
}
variable "work_cidr" {}
variable "key_pair_name" {}
variable "mssql_user" {}
variable "db_user" {}
variable "db_port" {}
variable "kafka_url" {}
variable "kafka_jdbc_connector" {}
variable "mssql_jdbc_driver" {}
variable "tags" {
    type = map(string)
}
